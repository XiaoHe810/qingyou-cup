"""Acceptance run against a disposable database using the real workbook."""
import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import timedelta
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Barrier
from unittest.mock import patch

from openpyxl import load_workbook
from werkzeug.security import generate_password_hash

from app import create_app, restore
import roster
import scheduling as sched
import tournament as t

TEST_PASSWORD = "acceptance-test-only"
TEST_SECRET = "isolated-acceptance-test-secret-not-production"


class Client:
    def __init__(self, app):
        self.client = app.test_client()
        self.csrf = self.get("/api/session")["csrf"]

    def get(self, path, status=200):
        response = self.client.get(path)
        assert response.status_code == status, (path, response.status_code, response.get_data()[:250])
        return response.json

    def post(self, path, data, status=200, upload=False):
        headers = {"X-CSRF-Token": self.csrf}
        response = (self.client.post(path, data=data, headers=headers) if upload else
                    self.client.post(path, json=data, headers=headers))
        assert response.status_code == status, (path, data.keys(), response.status_code, response.get_data()[:350])
        return response.json

    def export(self, path):
        response = self.client.get(path)
        assert response.status_code == 200, (path, response.status_code, response.get_data()[:300])
        return response.data


def verify(workbook, db_path, restored_path, output=None):
    app = create_app(db_path, TEST_SECRET)
    app.testing = True
    with t.transaction(db_path, True) as db:
        db.execute("INSERT INTO users(username,password_hash) VALUES(?,?)",
                   ("acceptance", generate_password_hash(TEST_PASSWORD)))
    admin, viewer, ref = (Client(app) for _ in range(3))
    admin.post("/api/login", {"username": "acceptance", "password": TEST_PASSWORD})
    room = admin.post("/api/rooms", {"workbook": (BytesIO(workbook.read_bytes()), "roster.xlsx"),
                                    "title": "青油杯 · 测试签位与赛程"}, upload=True)["room"]
    url = "/api/r/" + room
    assert viewer.client.get("/r/" + room).status_code == 200
    assert viewer.get(url + "/admin", 403)
    assert viewer.client.post(url + "/admin/draw", json={"project": "M", "action": "open"}).status_code == 403
    source = admin.get(url + "/admin")
    state = viewer.get(url)
    assert len(state["teams"]) == 31
    assert set(next(iter(state["teams"].values()))) == {"id", "college", "project"}
    assert Counter(x["project"] for x in state["teams"].values()) == roster.EXPECTED
    assert viewer.post("/api/join", {"room_code": source["join_code"]})["room"] == room
    assert len(source["source"]["cross_project"]) == 10
    assert sum(not x["verified"] for x in source["teams"]) == 4
    assert viewer.post(url + "/represent", {"college": "不在名单的学院", "name": "访客"}, 409)
    print("PASS 导入 31 队 / 110 条记录、4 对混双仍待核验、公开字段白名单与管理员权限", flush=True)

    def register(college):
        client = Client(app)
        data = {"college": college, "name": "验收代表 " + college}
        client.post(url + "/represent", data)
        return college, (client, data)
    colleges = {team["college"] for team in source["teams"]}
    with ThreadPoolExecutor(max_workers=11) as pool:
        registrations = dict(pool.map(register, sorted(colleges)))
    reps = {team["id"]: registrations[team["college"]] for team in source["teams"]}
    assert len(admin.get(url + "/admin")["participants"]) == 31
    college = "高等教育研究中心"
    viewer.post(url + "/represent", {"college": college, "name": "另一联系人"}, 409)
    registrations[college][0].post(url + "/draw", {"project": "X", "revision": 1}, 403)
    admin.post(url + "/admin/represent", {"college": college, "reason": "验收重置登记"})
    registrations[college][0].post(url + "/draw", {"project": "M", "revision": 1}, 403)
    registrations[college][0].post(url + "/represent", registrations[college][1])
    for p in t.PROJECTS:
        current = viewer.get(url)["projects"][p]
        order = current["order"][::-1]
        if p == "M":
            admin.post(url + "/admin/draw", {"project": p, "action": "order", "order": order})
        else:
            assert not current["ordered"]
        admin.post(url + "/admin/draw", {"project": p, "action": "open"})
        if p == "M":
            admin.post(url + "/admin/draw", {"project": p, "action": "pause"})
            reps[order[0]][0].post(url + "/draw", {"project": p, "revision": 1}, 409)
            admin.post(url + "/admin/draw", {"project": p, "action": "open"})
            reps[order[1]][0].post(url + "/draw", {"project": p, "revision": 1}, 409)
            rejoined = Client(app)
            rejoined.post(url + "/represent", reps[order[0]][1])
            first = reps[order[0]][0]
            barrier = Barrier(2)
            def race(client):
                barrier.wait()
                return client.client.post(url + "/draw", json={"project": p, "revision": 1},
                                          headers={"X-CSRF-Token": client.csrf}).status_code
            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(race, (first, rejoined)))
            assert sorted(results) == [200, 409]
            to_draw = order[1:]
        else:
            to_draw = order
        for team in to_draw:
            reps[team][0].post(url + "/draw", {"project": p, "revision": 1})
        reconnected = Client(create_app(db_path, TEST_SECRET))
        before = reconnected.get(url)["projects"][p]
        assert len(before["draws"]) == roster.EXPECTED[p]
        assert len({x["slot"] for x in before["draws"]}) == roster.EXPECTED[p]
        assert [x["team"] for x in before["draws"]] == order
        assert len({(n - 1)//2 for n in before["byes"]}) == 16 - roster.EXPECTED[p]
        assert reconnected.get(url)["projects"][p] == before
        reps[order[-1]][0].post(url + "/draw", {"project": p, "revision": 1}, 409)
        assert before["state"] == "locked"
    state = viewer.get(url)
    assert sum(not x["verified"] for x in admin.get(url + "/admin")["teams"]) == 4
    assert len(state["matches"]) == 37
    expected = {"M": {"pre": 3, "quarter": 4, "round": 6},
                "W": {"pre": 3, "quarter": 4, "round": 6},
                "X": {"pre": 1, "quarter": 4, "round": 6}}
    for p in t.PROJECTS:
        assert Counter(m["stage"] for m in state["matches"] if m["project"] == p) == expected[p]
        assert all(m["next"] for m in state["matches"] if m["project"] == p and m["stage"] != "round")
        assert len({frozenset((m["a_source"], m["b_source"])) for m in state["matches"]
                    if m["project"] == p and m["stage"] == "round"}) == 6
    admin.post(url + "/admin/draw", {"project": "X", "action": "redraw", "reason": "验收历史归档"}, 409)
    admin.post(url + "/admin/draw", {"project": "X", "action": "redraw", "reason": "验收历史归档", "confirm": "确认重抽"})
    admin.post(url + "/admin/draw", {"project": "X", "action": "open"})
    p = "X"
    for team in state["projects"]["X"]["order"]:
        reps[team][0].post(url + "/draw", {"project": p, "revision": 1}, 409)
        reps[team][0].post(url + "/draw", {"project": p, "revision": 2})
    assert viewer.get(url)["projects"]["X"]["state"] == "locked"
    with t.transaction(db_path) as db:
        assert db.execute("SELECT count(*) FROM draws WHERE project='X'").fetchone()[0] == 18
    print("PASS 11 学院并发登记、按序/自由抽签、抽签竞态、刷新与新连接恢复、暂停、抽齐自动锁定、重抽留痕及 3/3/1 附加赛", flush=True)

    admin.post(url + "/admin/officials", {"names": ["测试裁判甲", "测试裁判乙", "测试裁判丙", "测试裁判丁"]})
    admin.post(url + "/admin/schedule", {"action": "build"})
    astate = admin.get(url + "/admin")
    assert len(astate["draft"]) == 37 and len({x["start"][:10] for x in astate["draft"]}) == 2
    assert not any(m["schedule"] for m in viewer.get(url)["matches"])
    with t.transaction(db_path) as db:
        ctx = sched.match_context(db, room)
        sched.check(astate["draft"], ctx, astate["settings"]["rest"])
        p, q = next((a,b) for a in ctx.values() for b in ctx.values() if a["project"] != b["project"]
                    and a["players"] & b["players"])
        sample = {"start": "2026-11-14T08:00+08:00", "end": "2026-11-14T08:35+08:00", "referee": None}
        assert "跨项目" in sched.conflict(dict(sample, code=p["code"], court=1),
                                        dict(sample, code=q["code"], court=2), ctx, 5)
        assert "裁判" in sched.conflict(dict(sample, code=p["code"], court=1, referee="a"),
                                       dict(sample, code=q["code"], court=2, referee="a"), ctx, 5)
    a, b = astate["draft"][:2]
    admin.post(url + "/admin/schedule", {"action": "edit", "items": [dict(b, start=a["start"], court=a["court"])]}, 409)
    admin.post(url + "/admin/schedule", {"action": "publish", "confirmed": False}, 409)
    admin.post(url + "/admin/schedule", {"action": "publish", "confirmed": True})
    scheduled = sorted(viewer.get(url)["matches"], key=lambda m: (m["schedule"]["start"], m["code"]))
    assert len(scheduled) == 37
    first = scheduled[0]
    ref.post(url + "/referee", {"invite": next(o["invite_code"] for o in astate["officials"]
                                              if o["id"] == first["schedule"]["referee"])})
    viewer.post(url + "/match", {"code": first["code"], "action": "start"}, 403)
    ref.post(url + "/match", {"code": first["code"], "action": "start"})
    later = next(m for m in scheduled[1:] if m["a"] and m["b"] and
                 m["schedule"]["court"] == first["schedule"]["court"])
    blocked = admin.post(url + "/match", {"code": later["code"], "action": "start"}, 409)
    assert first["code"] in blocked["error"]
    ref.post(url + "/match", {"code": first["code"], "action": "submit", "forfeit": "b"})
    assert next(m for m in viewer.get(url)["matches"] if m["code"] == first["code"])["status"] == "playing"
    admin.post(url + "/match", {"code": first["code"], "action": "confirm"})

    for index, initial in enumerate(scheduled):
        if initial["code"] == first["code"]:
            continue
        match = next(m for m in viewer.get(url)["matches"] if m["code"] == initial["code"])
        assert match["a"] and match["b"], match
        clock = sched.local_time("2026-11-14T08:00") + timedelta(hours=index)
        with patch("tournament.now", return_value=clock.isoformat()), patch("scheduling.now", return_value=clock.isoformat()):
            # Keep the first result's actual timestamp on the simulation clock too.
            if index == 1:
                with t.transaction(db_path, True) as db:
                    db.execute("UPDATE matches SET finished=? WHERE room=? AND code=?",
                               ("2026-11-14T08:00:00+08:00", room, first["code"]))
            admin.post(url + "/match", {"code": match["code"], "action": "start"})
            p = match["project"]
            fmt = astate["settings"]["formats"][p]
            side = "a"
            if p == "X" and match["stage"] == "round":
                order = sorted({m["a"] for m in viewer.get(url)["matches"] if m["project"] == p and m["stage"] == "round"} |
                               {m["b"] for m in viewer.get(url)["matches"] if m["project"] == p and m["stage"] == "round"})
                i, j = order.index(match["a"]), order.index(match["b"])
                side = "a" if j == 3 or i != 3 and (j - i) % 3 == 1 else "b"
            elif match["stage"] == "round":
                side = "a" if match["a"] < match["b"] else "b"
            high, low = 11, index % 8
            if p == "X" and match["stage"] == "round":
                low = 8 - 2 * (i if side == "a" else j)
            pair = [high, low] if side == "a" else [low, high]
            rubbers = [[pair for _ in range(fmt["games"]//2 + 1)] for _ in range(fmt["rubbers"]//2 + 1)]
            admin.post(url + "/match", {"code": match["code"], "action": "submit", "confirmed": True, "rubbers": [[[11,10]]]}, 409)
            admin.post(url + "/match", {"code": match["code"], "action": "submit", "confirmed": True, "rubbers": rubbers})
    state = viewer.get(url)
    for p in t.PROJECTS:
        finals = [m for m in state["matches"] if m["project"] == p and m["stage"] == "round"]
        assert len({frozenset([m["a"],m["b"]]) for m in finals}) == 6
        assert all(m["status"] == "done" and m["a"] and m["b"] for m in finals)
        assert sorted(r["played"] for r in state["projects"][p]["standings"]) == [3] * 4
    assert not state["projects"]["X"]["ranking"]
    tied = [r["team"] for r in state["projects"]["X"]["standings"]]
    admin.post(url + "/admin/tie-decision", {"project": "X", "order": tied[::-1],
               "basis": "验收模拟裁决", "confirmed": True}, 409)
    admin.post(url + "/admin/tie-decision", {"project": "X", "order": tied,
               "basis": "验收模拟裁决", "confirmed": False}, 409)
    admin.post(url + "/admin/tie-decision", {"project": "X", "order": tied,
               "basis": "验收模拟裁决", "confirmed": True})
    assert len(viewer.get(url)["projects"]["X"]["ranking"]) == 4
    changed = deepcopy(astate["settings"])
    changed["tie"] = ["point_diff", "head_to_head"]
    admin.post(url + "/admin/settings", changed, 409)
    changed.update(confirm_tie=True, tie_basis="验收模拟同分规则")
    admin.post(url + "/admin/settings", changed)
    state = viewer.get(url)
    assert all(len(p["ranking"]) == 4 for p in state["projects"].values()), state["projects"]["X"]["standings"]
    admin.post(url + "/match", {"code": first["code"], "action": "submit", "confirmed": True, "forfeit": "a", "reason": "已有下游赛况"}, 409)
    admin.post(url + "/match", {"code": first["code"], "action": "submit", "confirmed": True, "forfeit": "b", "reason": "验收成绩修订"})
    assert any(x["action"] == "比赛成绩或状态" and x["detail"]["reason"] == "验收成绩修订"
               and x["detail"]["before"] and x["detail"]["after"] for x in admin.get(url + "/audit"))
    print("PASS 两天 37 场排程、实际开赛拦截、裁判确认、逐局计分、八强/四强晋级、各 6 场及同分确认", flush=True)

    from exports import REPORTS
    for key in REPORTS:
        for ext in ("xlsx", "pdf"):
            data = viewer.export(url + f"/export/{key}/{ext}")
            if ext == "xlsx":
                sheet = load_workbook(BytesIO(data), rich_text=True).active
                assert str(sheet.page_setup.paperSize) == str(sheet.PAPERSIZE_A4)
                if key in ("schedule", "matchups"):
                    assert sheet.max_row == 40
                if key in t.PROJECTS:
                    assert sheet.max_row == 3 + sum(expected[key].values())
            else:
                assert data.startswith(b"%PDF")
            if output and key in ("draw", "M", "W", "X", "schedule", "matchups"):
                (output / f"测试_{REPORTS[key]}.{ext}").write_bytes(data)
    backup = restored_path.with_name("backup.sqlite3")
    backup.write_bytes(admin.export("/api/backup"))
    restore(backup, restored_path)
    reboot = Client(create_app(restored_path, TEST_SECRET))
    assert reboot.get(url) == viewer.get(url)
    with t.transaction(restored_path) as db:
        assert db.execute("SELECT count(*) FROM draws").fetchone()[0] == 40
        assert db.execute("SELECT count(*) FROM rankings").fetchone()[0] == 12
    print("PASS 20 个 Excel/PDF 导出、数据库在线备份、异库恢复与全量状态对比", flush=True)
    return room


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("workbook", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.output:
        args.output.mkdir(parents=True, exist_ok=True)
    runtime = Path("runtime")
    runtime.mkdir(exist_ok=True)
    with TemporaryDirectory(dir=runtime) as temp:
        p = Path(temp)
        verify(args.workbook, p / "active.sqlite3", p / "restored.sqlite3", args.output)
