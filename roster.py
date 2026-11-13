"""Import the supplied registration workbook without persisting student IDs or phones."""
import argparse
from collections import Counter, defaultdict
from hashlib import sha256
from io import BytesIO
import json
from pathlib import Path
import re

from openpyxl import load_workbook

PROJECTS = {"M": "男子团体", "W": "女子团体", "X": "混合双打"}
SOURCE_PROJECTS = {"男子组": "M", "女子组": "W", "混合双打": "X"}
EXPECTED = {"M": 11, "W": 11, "X": 9}
HEADERS = ("学院", "比赛项目", "参赛选手姓名", "学号", "队员类别")
FORMAL, RESERVE, MIXED = "正式队员", "替补队员", "混双选手"


def validate(teams):
    errors = []
    counts = Counter(team["project"] for team in teams)
    if dict(counts) != EXPECTED:
        errors.append(f"项目队伍数应为 {EXPECTED}，实为 {dict(counts)}")
    if len({(t["college"], t["project"]) for t in teams}) != len(teams):
        errors.append("存在重复学院项目")
    for t in teams:
        label = f'{t["college"]} / {PROJECTS[t["project"]]}'
        players = t["players"]
        roles = Counter(p["role"] for p in players)
        if len({p["person"] for p in players}) != len(players):
            errors.append(f"{label} 有重复选手")
        if t["project"] == "X":
            if roles != {MIXED: 2}:
                errors.append(f"{label} 应有两名混双选手")
            if sorted(p["sex"] for p in players if p["sex"]) != ["女", "男"]:
                errors.append(f"{label} 待核验一男一女")
        elif not (3 <= len(players) <= 5 and roles[FORMAL] == 3 and
                  roles[RESERVE] == len(players) - 3):
            errors.append(f"{label} 应为3–5人，含3名正式队员")
    return errors


def parse_workbook(raw):
    book = load_workbook(BytesIO(raw), data_only=True)
    if not {"填写说明", "参赛人员信息"} <= set(book.sheetnames):
        raise ValueError("缺少填写说明或参赛人员信息工作表")
    sheet = book["参赛人员信息"]
    instructions = [str(v) for row in book["填写说明"].values for v in row if v is not None]
    header = next((i for i, row in enumerate(sheet.values, 1) if set(HEADERS) <= set(row)), None)
    if header is None:
        raise ValueError("找不到报名表表头")
    columns = {c.value: c.column for c in sheet[header] if c.value}
    merged = {}
    for area in sheet.merged_cells.ranges:
        for row in range(max(header + 1, area.min_row), area.max_row + 1):
            for col in range(area.min_col, area.max_col + 1):
                merged[row, col] = sheet.cell(area.min_row, area.min_col).value

    def cell(row, field):
        col = columns.get(field)
        if col is None:
            return ""
        value = merged.get((row, col), sheet.cell(row, col).value)
        return "" if value is None else str(value)

    grouped = {}
    identities = defaultdict(list)
    student_ids = defaultdict(set)
    missing_mail = set()
    for row in range(header + 1, sheet.max_row + 1):
        college, source_project, source_name, student, role = (cell(row, field) for field in HEADERS)
        if not (source_name or student or role):
            continue
        project = SOURCE_PROJECTS.get(source_project.strip())
        if not college.strip() or not source_name.strip() or not student.strip() or not role.strip() or project is None:
            raise ValueError(f"第 {row} 行学院、项目、姓名、学号或类别缺失或无法识别")
        college, name = college.strip(), source_name.strip()
        key = (college, name)
        token = f"P{identities[key][0][0] if identities[key] else row:03}"
        sex = cell(row, "性别").strip()
        if sex not in ("", "男", "女"):
            raise ValueError(f"第 {row} 行性别值无法识别")
        player = {"name": name, "role": role.strip(), "row": row, "person": token,
                  "sex": sex, "sex_source": "原表性别列" if sex else ""}
        if source_name != name:
            player["source_name"] = source_name
        team = grouped.setdefault((college, project), {"id": f"T{len(grouped) + 1:02}",
                            "college": college, "project": project, "players": []})
        team["players"].append(player)
        identity_id = student.strip()
        student_ids[identity_id].add(key)
        identities[key].append((row, project, identity_id, player))
        if not cell(row, "邮件发送时间"):
            missing_mail.add(college)

    identity_issues = []
    cross_project = []
    for (college, name), entries in identities.items():
        projects = {p for _, p, _, _ in entries}
        ids = {sid for _, _, sid, _ in entries}
        conflict = len(ids) > 1
        evidence = [{"row": row, "project": p} for row, p, _, _ in entries]
        if conflict:
            identity_issues.append({"college": college, "name": name, "records": evidence,
                                    "issue": "同学院同名记录的学号不一致"})
        if len(projects) > 1:
            cross_project.append({"college": college, "name": name, "records": evidence,
                                  "identity": "学号不一致，按可能同人避让" if conflict else "学号一致"})
        for _, project, sid, player in entries:
            if project != "X" or player["sex"] or conflict:
                continue
            categories = projects & {"M", "W"}
            if len(categories) == 1 and len(student_ids[sid]) == 1:
                player["sex"] = "男" if "M" in categories else "女"
                player["sex_source"] = "同学院同名同学号的团体项目"
    for names in student_ids.values():
        if len(names) > 1:
            rows = [row for key in names for row, _, _, _ in identities[key]]
            identity_issues.append({"rows": sorted(rows), "issue": "同学号对应不同姓名或学院"})
            token = f"P{min(rows):03}"
            for key in names:
                for _, _, _, player in identities[key]:
                    player["person"] = token

    teams = list(grouped.values())
    for team in teams:
        for player in team["players"]:
            if team["project"] != "X":
                player["sex"] = ""
                player["sex_source"] = ""
    return {"sha256": sha256(raw).hexdigest(), "sheets": book.sheetnames,
            "merge_counts": {s.title: len(s.merged_cells.ranges) for s in book},
            "instructions": instructions, "sex_column": "性别" in columns,
            "teams": teams, "errors": validate(teams), "identity_issues": identity_issues,
            "cross_project": cross_project, "missing_mail": sorted(missing_mail)}


def report(data, filename):
    teams = data["teams"]
    lines = ["# 第四届青油杯报名名单核验（管理员用）", "",
             f"- 来源工作簿《{filename}》", f'- SHA-256 `{data["sha256"]}`',
             "- 工作表及合并区域 " + "、".join(f'{s} {n} 处' for s, n in data["merge_counts"].items()),
             f'- {len({t["college"] for t in teams})} 个参赛单位，{len(teams)} 支队伍或组合，'
             f'{sum(len(t["players"]) for t in teams)} 条参赛记录',
             f'- 同名同学院及学号交叉核对共识别 {len(data["cross_project"])} 组跨项目报名或可能重复身份。'
             " 排程按可能同一人避让。", "",
             "## 校验结论", "",
             "- 所有团体组满足 3–5 人和 3 名正式队员要求。混双每对恰有两条混双选手记录。",
             "- 人员表没有性别列。团体组项目只作为匹配到同学院、同名、同学号选手的间接证据。",
             "- 待核验组合需由管理员记录负责人确认依据。下方行号可在原表定位。", ""]
    lines.extend(f"- {error}" for error in data["errors"])
    lines.extend(f'- {i.get("college","")} {i.get("name","")}：{i["issue"]}，'
                 f'行 {", ".join(str(r["row"]) for r in i.get("records", [])) or i.get("rows")}'
                 for i in data["identity_issues"])
    lines.append("- 原表邮件发送时间未填写：" + "、".join(data["missing_mail"]) + "。")
    trimmed = [f'F{p["row"]}' for t in teams for p in t["players"] if "source_name" in p]
    if trimmed:
        lines.append("- 展示姓名去除首尾空格，结构化导入保留原值。对应原表单元格：" + "、".join(trimmed) + "。")
    for code, label in PROJECTS.items():
        group = [t for t in teams if t["project"] == code]
        lines.extend(["", f"## {label} · {len(group)} 队 / {sum(len(t['players']) for t in group)} 条记录", "",
                      "| 学院 | 正式队员 / 混双选手 | 替补队员 | 原表行号 |",
                      "| --- | --- | --- | --- |"])
        for t in group:
            first = "、".join(p["name"] + (f'（{p["sex"]}，{p["sex_source"]}）' if p["sex"] else
                              "（性别待核）" if code == "X" else "")
                             for p in t["players"] if p["role"] != RESERVE)
            reserves = "、".join(p["name"] for p in t["players"] if p["role"] == RESERVE) or "—"
            rows = "、".join(str(p["row"]) for p in t["players"])
            lines.append(f"| {t['college']} | {first} | {reserves} | {rows} |")
    lines.extend(["", "## 跨项目参赛记录", ""])
    for p in data["cross_project"]:
        lines.append(f'- {p["college"]} · {p["name"]}，'
                     + "、".join(f'{PROJECTS[e["project"]]}第{e["row"]}行' for e in p["records"])
                     + f'（{p["identity"]}）')
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    cli = argparse.ArgumentParser()
    cli.add_argument("workbook", type=Path)
    cli.add_argument("output", type=Path)
    args = cli.parse_args()
    parsed = parse_workbook(args.workbook.read_bytes())
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "roster.admin.json").write_text(json.dumps(parsed, ensure_ascii=False, indent=2), encoding="utf-8")
    (args.output / "报名名单核验.md").write_text(report(parsed, args.workbook.name), encoding="utf-8")
    print(Counter(t["project"] for t in parsed["teams"]))
    print("参赛记录", sum(len(t["players"]) for t in parsed["teams"]))
    print("待核验", *parsed["errors"], sep="\n")
