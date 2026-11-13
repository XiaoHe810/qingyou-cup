"""Draft schedule, conservative unresolved-bracket conflicts and actual start checks."""
from datetime import datetime, timedelta
from itertools import combinations

from tournament import PROJECTS, TZ, DEFAULTS, need, rows, one, loads, dumps, log, now

TIE_RULES = {"head_to_head": "两队直接交锋", "rubber_diff": "净胜单项", "game_diff": "净胜局", "point_diff": "净胜分"}


def config(payload, old):
    def integer(value, low, high, label):
        need(type(value) is int and low <= value <= high, f"{label}超出允许范围")
        return value

    venue = payload["venue"].strip()
    need(0 < len(venue) <= 120, "请填写有效比赛地点")
    settings = {**old, "venue": venue}
    for field, low, high, label in (("courts", 1, 64, "球台"), ("minutes", 1, 720, "预计用时"),
                                    ("rest", 0, 180, "休息时间")):
        settings[field] = integer(payload[field], low, high, label)
    sessions = payload["sessions"]
    need(isinstance(sessions, list) and 1 <= len(sessions) <= 30, "需要1–30个场次")
    windows = session_windows(sessions)
    need(all(a < b for a, b in windows) and all(a[1] <= b[0] for a, b in zip(windows, windows[1:])),
         "场次时间无效或相互重叠")
    settings["sessions"] = sessions
    settings["final_day"] = payload.get("final_day") is True
    settings["points"] = {key: integer(payload["points"][key], 0, 100, "积分")
                          for key in DEFAULTS["points"]}
    tie = payload["tie"]
    need(isinstance(tie, list) and len(tie) == len(set(tie)) and all(x in TIE_RULES for x in tie), "同分规则无效")
    settings["tie"] = tie
    formats = payload["formats"]
    for p in PROJECTS:
        item = formats[p]
        need(item["rubbers"] in (1, 3, 5, 7) and item["games"] in (1, 3, 5, 7), "赛制局数必须为1、3、5或7")
        need(p != "X" or item["rubbers"] == 1, "混双每场为一个单项")
    settings["formats"] = formats
    return settings


def local_time(value):
    need(isinstance(value, str), "时间格式无效")
    result = datetime.fromisoformat(value)
    return result.replace(tzinfo=TZ) if result.tzinfo is None else result.astimezone(TZ)


def session_windows(sessions):
    return sorted((local_time(s["date"] + "T" + s["start"]), local_time(s["date"] + "T" + s["end"]))
                  for s in sessions)


def match_context(db, room):
    matches = rows(db, """SELECT * FROM matches WHERE room=?
                         ORDER BY CASE stage WHEN 'pre' THEN 0 WHEN 'quarter' THEN 1 ELSE 2 END,code""", (room,))
    team_players = {t["id"]: {p["person"] for p in loads(t["players"])}
                    for t in rows(db, "SELECT id,players FROM teams WHERE room=?", (room,))}
    options = {}
    for m in matches:
        deps = {x for x in (m["a_source"], m["b_source"]) if not x.startswith("T")}
        teams = set()
        for side in ("a", "b"):
            source = m[side + "_source"]
            if m[side]:
                teams.add(m[side])
            else:
                teams.update(options[source]["teams"])
        options[m["code"]] = {**m, "teams": teams, "players": set().union(*(team_players[t] for t in teams)),
                              "deps": deps}
    return options


def assignments(db, room, kind):
    return rows(db, "SELECT code,start,end,court,referee FROM schedule WHERE room=? AND kind=? ORDER BY start,court",
                (room, kind))


def normalized(item, settings, officials):
    start = local_time(item["start"])
    end = start + timedelta(minutes=settings["minutes"])
    court, referee = item["court"], item.get("referee") or None
    need(type(court) is int and 1 <= court <= settings["courts"], "球台编号超出设置范围")
    need(referee is None or referee in officials, "裁判不在裁判名单内")
    need(any(a <= start and end <= b for a, b in session_windows(settings["sessions"])), "时间超出比赛场次")
    return {"code": item["code"], "start": start.isoformat(timespec="minutes"),
            "end": end.isoformat(timespec="minutes"), "court": court, "referee": referee}


def conflict(a, b, options, rest):
    am, bm = options[a["code"]], options[b["code"]]
    start_a, end_a, start_b, end_b = map(local_time, (a["start"], a["end"], b["start"], b["end"]))
    if bm["code"] in am["deps"] and end_b > start_a or am["code"] in bm["deps"] and end_a > start_b:
        return "晋级前置比赛时间冲突"
    if start_a < end_b and start_b < end_a:
        if a["court"] == b["court"]:
            return "球台冲突"
        if a["referee"] and a["referee"] == b["referee"]:
            return "裁判冲突"
    gap = timedelta(minutes=rest)
    if start_a < end_b + gap and start_b < end_a + gap:
        if am["teams"] & bm["teams"]:
            return "同队比赛或休息时间冲突"
        if am["players"] & bm["players"]:
            return "跨项目选手比赛或休息时间冲突"
    return None


def check(items, options, rest):
    for a, b in combinations(items, 2):
        issue = conflict(a, b, options, rest)
        need(not issue, f'{a["code"]} / {b["code"]}：{issue}')


def save_draft(db, room, items):
    db.execute("DELETE FROM schedule WHERE room=? AND kind='draft'", (room,))
    db.executemany("INSERT INTO schedule VALUES(?,?,'draft',?,?,?,?)",
                   [(room, x["code"], x["start"], x["end"], x["court"], x["referee"]) for x in items])


def build_schedule(db, room, settings):
    options = match_context(db, room)
    need(options, "请先锁定抽签结果")
    officials = [r["id"] for r in rows(db, "SELECT id FROM officials WHERE room=? ORDER BY name", (room,))]
    previous = {r["code"]: r for r in assignments(db, room, "draft")}
    published = {r["code"]: r for r in assignments(db, room, "public")}
    items = [published.get(k) or previous.get(k) for k, m in options.items() if m["status"] != "pending"]
    need(all(items), "已有比赛缺少日程，请先补齐")
    windows = session_windows(settings["sessions"])
    duration = timedelta(minutes=settings["minutes"])
    for k, m in options.items():
        if m["status"] != "pending":
            continue
        chosen = None
        preferred = windows
        if m["stage"] == "round" and settings.get("final_day"):
            preferred = sorted(windows, key=lambda w: w[0].date() != windows[-1][0].date())
        for first, last in preferred:
            at = first
            while not chosen and at + duration <= last:
                for court in range(1, settings["courts"] + 1):
                    for referee in officials or [None]:
                        x = {"code": k, "start": at.isoformat(timespec="minutes"),
                             "end": (at + duration).isoformat(timespec="minutes"),
                             "court": court, "referee": referee}
                        if all(not conflict(x, y, options, settings["rest"]) for y in items):
                            chosen = x
                            break
                    if chosen:
                        break
                at += timedelta(minutes=1)
            if chosen:
                break
        need(chosen, f"现有时段无法排入 {k}，请调整球台、预计用时、休息或场次")
        items.append(chosen)
    save_draft(db, room, items)
    return items


def edit_draft(db, room, settings, edits):
    options = match_context(db, room)
    officials = {r["id"] for r in rows(db, "SELECT id FROM officials WHERE room=?", (room,))}
    items = {x["code"]: x for x in assignments(db, room, "draft")}
    for edit in edits:
        k = edit["code"]
        need(k in options and options[k]["status"] == "pending", "只可调整尚未开始的比赛")
        items[k] = normalized(edit, settings, officials)
    check(list(items.values()), options, settings["rest"])
    save_draft(db, room, items.values())


def publish_schedule(db, room, settings):
    options = match_context(db, room)
    items = assignments(db, room, "draft")
    need(items and set(options) == {x["code"] for x in items}, "待确认日程未覆盖已生成的全部比赛")
    need(one(db, "SELECT count(*) AS n FROM projects WHERE room=? AND state='locked'", (room,))["n"] == 3,
         "三个项目全部锁定后才能发布完整比赛日程")
    officials = {r["id"] for r in rows(db, "SELECT id FROM officials WHERE room=?", (room,))}
    for item in items:
        need(item["referee"], f'{item["code"]} 尚未安排裁判')
        if options[item["code"]]["status"] == "pending":
            need(normalized(item, settings, officials) == item, "设置已改变，请重新排程")
    check(items, options, settings["rest"])
    db.execute("DELETE FROM schedule WHERE room=? AND kind='public'", (room,))
    db.executemany("INSERT INTO schedule VALUES(?,?,'public',?,?,?,?)",
                   [(room, x["code"], x["start"], x["end"], x["court"], x["referee"]) for x in items])


def can_start(db, room, match, settings):
    need(match["a"] and match["b"], "双方尚未确定")
    options = match_context(db, room)
    current = options[match["code"]]
    plan = {s["code"]: s for s in assignments(db, room, "public")}
    need(match["code"] in plan, "请先发布该场日程")
    for key, m in options.items():
        if key == match["code"]:
            continue
        overlap = current["players"] & m["players"] or current["teams"] & m["teams"]
        if m["status"] == "playing":
            other = plan.get(key)
            shared = other and (other["court"] == plan[match["code"]]["court"] or
                                other["referee"] and other["referee"] == plan[match["code"]]["referee"])
            need(not (overlap or shared), f"{key} 尚未结束，选手、球台或裁判有冲突")
        if m["status"] == "pending" and key in plan and overlap:
            need(plan[key]["start"] >= plan[match["code"]]["start"], f"{key} 排在前面且尚未结束")
        if m["finished"] and overlap:
            need(local_time(now()) >= local_time(m["finished"]) + timedelta(minutes=settings["rest"]),
                 f"{key} 结束后的休息时间未满")
