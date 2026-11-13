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


def set_order(db, room, project, order, actor, ordered=True):
    p = project_state(db, room, project)
    need(p["state"] == "waiting" and not get_draws(db, p), "仅可在项目开放抽签前设置顺序")
    need(isinstance(order, list) and all(isinstance(x, str) for x in order)
         and sorted(order) == sorted(p["draw_order"]), "顺序必须包含该项目全部学院，且各出现一次")
    db.execute("UPDATE projects SET draw_order=? WHERE room=? AND code=?", (dumps(order), room, project))
    settings = get_room(db, room)["settings"]
    ordered_projects = set(settings.get("ordered_projects", []))
    if ordered:
        ordered_projects.add(project)
    else:
        ordered_projects.discard(project)
    settings["ordered_projects"] = sorted(ordered_projects)
    db.execute("UPDATE rooms SET settings=? WHERE id=?", (dumps(settings), room))
    log(db, room, actor, "修改抽签顺序", {"project": project, "before": p["draw_order"], "after": order,
                                        "ordered": ordered})


def draw_action(db, room, project, action, actor, reason="", confirm=""):
    p = project_state(db, room, project)
    current = get_draws(db, p)
    state, byes, revision = p["state"], p["byes"], p["revision"]
    if action == "open":
        need(state in ("waiting", "paused") and len(current) < len(p["draw_order"]), "当前无法开放抽签")
        count = one(db, "SELECT count(*) AS n FROM teams WHERE room=? AND project=?",
                    (room, project))["n"]
        need(count == EXPECTED[project], "该项目报名队数与赛制不符")
        if not byes:
            byes = sorted(2 * pair + RNG.choice((1, 2)) for pair in RNG.sample(range(8), 16 - count))
        state = "open"
    elif action == "pause":
        need(state == "open", "当前无法暂停")
        state = "paused"
    elif action == "lock":
        need(state in ("open", "paused") and len(current) == len(p["draw_order"]), "请等待全部队伍完成抽签")
        create_matches(db, room, project, current, byes)
        state = "locked"
    elif action == "redraw":
        need(state != "waiting", "该项目尚未开启")
        need(confirm == "确认重抽" and reason.strip(), "请输入重抽原因，并输入「确认重抽」")
        need(not one(db, """SELECT 1 FROM matches WHERE room=? AND project=?
                            AND (status!='pending' OR pending_score IS NOT NULL)""", (room, project)),
             "该项目已有开赛或成绩记录，不能重抽")
        old = rows(db, "SELECT * FROM matches WHERE room=? AND project=?", (room, project))
        assignments = rows(db, """SELECT s.* FROM schedule s JOIN matches m ON m.room=s.room AND m.code=s.code
                               WHERE m.room=? AND m.project=?""", (room, project))
        log(db, room, actor, "重抽归档", {"project": project, "revision": revision, "reason": reason.strip(),
                                        "byes": byes, "draws": current, "matches": old, "schedule": assignments})
        db.execute("DELETE FROM matches WHERE room=? AND project=?", (room, project))
        revision, state, byes = revision + 1, "waiting", []
    else:
        raise RuleError("未知抽签操作")
    db.execute("UPDATE projects SET state=?,byes=?,revision=? WHERE room=? AND code=?",
               (state, dumps(byes), revision, room, project))
    log(db, room, actor, "抽签状态", {"project": project, "action": action, "state": state,
                                   "revision": revision, "byes": byes})


def take_slot(db, room, project, team, actor):
    p = project_state(db, room, project)
    current = get_draws(db, p)
    need(team in p["draw_order"], "该项目报名队伍校验失败")
    need(not any(d["team"] == team for d in current), "该学院本轮已完成抽签")
    need(p["state"] == "open", "主持人尚未开放抽签或已暂停")
    if project in get_room(db, room)["settings"].get("ordered_projects", []):
        need(p["draw_order"][len(current)] == team, "请等待本学院抽签顺序")
    occupied = set(p["byes"]) | {d["slot"] for d in current}
    slot = RNG.choice(sorted(set(range(1, 17)) - occupied))
    at = now()
    db.execute("INSERT INTO draws VALUES(?,?,?,?,?,?,?,?)",
               (room, project, p["revision"], team, slot, len(current) + 1, at, actor))
    log(db, room, actor, "抽签", {"project": project, "revision": p["revision"],
                               "team": team, "slot": slot, "seq": len(current) + 1})
    if len(current) + 1 == len(p["draw_order"]):
        draw_action(db, room, project, "lock", "系统")
    return slot


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
