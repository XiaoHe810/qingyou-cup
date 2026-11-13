"""Printable public tournament reports, with A4 page setup."""
from io import BytesIO
import os
from pathlib import Path
import re
from xml.sax.saxutils import escape

from openpyxl import Workbook
from openpyxl.cell.rich_text import CellRichText, TextBlock
from openpyxl.cell.text import InlineFont
from openpyxl.styles import Alignment, Border, Font, Side
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.page import PageMargins
from reportlab.graphics.shapes import Drawing, Line, Rect, String
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from tournament import PROJECTS, STAGES, STATUSES, need

REPORTS = {"draw": "抽签结果", **{p: f"{name}完整对阵" for p, name in PROJECTS.items()},
           "matchups": "三项目完整交叉对打表", "schedule": "两天比赛日程", "referees": "裁判工作安排", "scores": "比赛成绩汇总",
           "standings": "四强循环赛积分表", "awards": "最终获奖名单"}
LATIN = re.compile(r"([ -~]+)")


def display_time(stamp):
    return stamp[:16].replace("T", " ") if stamp else ""


def score_text(score, project):
    if not score:
        return ""
    if score["forfeit"]:
        return ("甲" if score["forfeit"] == "a" else "乙") + "方弃权"
    return "-".join(map(str, score["games"] if project == "X" else score["total"]))


def match_side(m, side, names):
    source = m[side + "_source"]
    return names.get(m[side]) or (f"八强重抽 {source[1:]}号签" if source.startswith("Q") else source + " 胜者")


def table_data(state, kind):
    need(kind in REPORTS, "未知导出报表")
    names = {k: v["college"] for k, v in state["teams"].items()}
    matches = state["matches"]
    if kind == "draw":
        shared = state["college_draw"]
        if shared["draws"]:
            project_slots = {p: {d["college"]: d["slot"] for d in project["draws"]}
                             for p, project in state["projects"].items()}
            lines = [[d["position"], d["college"], *[project_slots[p].get(d["college"], "") for p in PROJECTS],
                      d["seq"], d["drawn_at"][:19].replace("T", " ")] for d in shared["draws"]]
            return ["学院顺序", "学院", *PROJECTS.values(), "操作次序", "抽签时间"], lines
        lines = []
        for p, project in state["projects"].items():
            for d in project["draws"]:
                lines.append([PROJECTS[p], project["revision"], d["seq"], d["slot"], d["college"], d["drawn_at"][:19].replace("T", " ")])
            for slot in project["byes"]:
                lines.append([PROJECTS[p], project["revision"], "", slot, "轮空 BYE", ""])
        return ["项目", "轮次", "顺序", "签位", "学院", "抽签时间"], lines
    if kind in PROJECTS or kind in ("scores", "matchups"):
        lines = []
        for m in matches:
            if kind in PROJECTS and m["project"] != kind:
                continue
            sides = [match_side(m, side, names) for side in ("a", "b")]
            score = score_text(m["score"], m["project"])
            if kind == "scores" and m["score"] and not m["score"]["forfeit"]:
                details = ["、".join(f"{a}-{b}" for a, b in rub) for rub in m["score"]["rubbers"]]
                score += " / " + "；".join(details)
            lines.append([m["code"], STAGES[m["stage"]], *sides, STATUSES[m["status"]], score,
                          names.get(m["winner"], ""), " / ".join(m["next"])])
        return ["编号", "阶段", "甲方", "乙方", "状态", "比分", "胜方", "晋级位置"], lines
    if kind in ("schedule", "referees"):
        lines = []
        for m in matches:
            s = m["schedule"]
            if not s:
                continue
            sides = [match_side(m, side, names) for side in ("a", "b")]
            lines.append([display_time(s["start"]), s["end"][11:16], s["court"], m["code"],
                          sides[0] + " / " + sides[1], s["official"] or ""])
        lines.sort(key=lambda item: (item[5], item[0], item[2]) if kind == "referees" else (item[0], item[2]))
        return ["开始（北京时间）", "预计结束", "台", "编号", "对阵（甲 / 乙）", "裁判"], lines
    lines = []
    for p, project in state["projects"].items():
        if kind == "awards":
            lines.extend([PROJECTS[p], r["rank"], names[r["team"]]] for r in project["ranking"])
        else:
            lines.extend([PROJECTS[p], names[r["team"]], r["played"], r["wins"], r["losses"],
                          r["forfeits"], r["points"], r["rank"] or ""] for r in project["standings"])
    return (["项目", "名次", "学院"] if kind == "awards" else
            ["项目", "学院", "场次", "胜", "负", "弃权", "积分", "名次"]), lines


def xlsx(title, subtitle, columns, lines, wide):
    book = Workbook()
    sheet = book.active
    sheet.title = title
    n = len(columns)
    sheet.append([title])
    sheet.merge_cells(start_row=1, start_column=1, end_row=1, end_column=n)
    sheet.append([subtitle])
    sheet.merge_cells(start_row=2, start_column=1, end_row=2, end_column=n)
    sheet.append(columns)
    for line in lines:
        sheet.append(line)
    if not lines:
        sheet.append(["暂无数据"])
    sheet.sheet_view.showGridLines = False
    sheet.freeze_panes = "A4"
    sheet.print_title_rows = "1:3"
    sheet.page_setup.paperSize = sheet.PAPERSIZE_A4
    sheet.page_setup.orientation = "landscape" if wide else "portrait"
    sheet.page_setup.fitToWidth = 1
    sheet.page_setup.fitToHeight = 0
    sheet.sheet_properties.pageSetUpPr.fitToPage = True
    sheet.page_margins = PageMargins(left=.3, right=.3, top=.35, bottom=.35, header=.1, footer=.15)
    side = Side(style="thin", color="000000")
    for row in sheet.iter_rows():
        for c in row:
            c.font = Font(name="SimSun" if isinstance(c.value, str) else "Times New Roman",
                          size=14 if c.row == 1 else 10, bold=c.row in (1, 3), color="000000")
            if isinstance(c.value, str) and c.row != 1:
                c.data_type = "s"
                if LATIN.search(c.value):
                    c.value = CellRichText([TextBlock(InlineFont(rFont="Times New Roman" if i % 2 else "SimSun",
                                                sz=10, b=c.row == 3, color="000000"), part)
                                            for i, part in enumerate(LATIN.split(c.value)) if part])
            c.alignment = Alignment(vertical="center", wrap_text=True, horizontal="left")
            if c.row >= 3:
                c.border = Border(left=side, right=side, top=side, bottom=side)
    widths = []
    for i, col in enumerate(columns):
        lengths = [sum(2 if ord(x) > 127 else 1 for x in str(line[i])) for line in [columns, *lines]]
        widths.append(max(8, min(36, max(lengths) + 2)))
        sheet.column_dimensions[get_column_letter(i + 1)].width = widths[i]
    for i, line in enumerate(lines, 4):
        sheet.row_dimensions[i].height = max(20, max(
            14 * (1 + sum(2 if ord(c) > 127 else 1 for c in str(v)) // max(4, widths[j] - 2))
            for j, v in enumerate(line)))
    sheet.row_dimensions[1].height = 28
    sheet.row_dimensions[2].height = 34
    sheet.print_area = f"A1:{get_column_letter(n)}{sheet.max_row}"
    out = BytesIO()
    book.save(out)
    out.seek(0)
    return out


def bracket_diagram(state, project, width):
    bracket = state["projects"][project]
    slots = {d["slot"]: d for d in bracket["draws"]}
    matches = [m for m in state["matches"] if m["project"] == project]
    pre = {frozenset((m["a_source"], m["b_source"])): m for m in matches if m["stage"] == "pre"}
    quarters = [m for m in matches if m["stage"] == "quarter"]
    names = {key: v["college"] for key, v in state["teams"].items()}
    chart = Drawing(width, 413)

    def label(x, y, value, size=10, font="Song"):
        chart.add(String(x, y, value, fontName=font, fontSize=size, fillColor=colors.black))

    def box(x, y, w, h):
        chart.add(Rect(x, y, w, h, fillColor=colors.white, strokeColor=colors.black, strokeWidth=.5))

    def line(x, y, a, b):
        chart.add(Line(x, y, a, b, strokeColor=colors.black, strokeWidth=.55))

    columns = (0, width * .31, width * .61)
    sizes = (width * .26, width * .25, width * .38)
    redraw = bracket["quarter"]["redraw"]
    last_title = ("八强重抽结果" if bracket["quarter"]["draw"] else "八强签位待重抽") if redraw else "八强晋级路线"
    for x, title in zip(columns, ("原始签位", "附加赛或轮空", last_title)):
        label(x, 398, title, 12)
    centers = [367 - i * 48 for i in range(8)]
    for pair, cy in enumerate(centers):
        a, b = (pair * 2 + offset for offset in (1, 2))
        left, right = slots.get(a), slots.get(b)
        box(columns[0], cy - 17, sizes[0], 36)
        for k, d, y in ((a, left, cy + 5), (b, right, cy - 10)):
            label(9, y, f"{k:02}", font="TNR")
            label(29, y, d["college"] if d else "轮空")
        line(sizes[0], cy, columns[1], cy)
        box(columns[1], cy - 17, sizes[1], 36)
        if left and right:
            m = pre[frozenset((left["team"], right["team"]))]
            label(columns[1] + 9, cy + 4, m["code"], font="TNR")
            label(columns[1] + 9, cy - 10, names.get(m["winner"], "胜者待定"))
        else:
            label(columns[1] + 9, cy + 4, "自动轮空晋级")
            label(columns[1] + 9, cy - 10, (left or right)["college"])
    for index, m in enumerate(quarters):
        top, bottom = centers[2*index:2*index+2]
        cy = (top + bottom) / 2
        start, end = columns[1] + sizes[1], columns[2]
        middle = (start + end) / 2
        if not redraw:
            line(start, top, middle, top)
            line(start, bottom, middle, bottom)
            line(middle, top, middle, bottom)
            line(middle, cy, end, cy)
        box(end, cy - 28, sizes[2], 56)
        label(end + 9, cy + 13, m["code"], font="TNR")
        if redraw:
            label(end + 43, cy + 13, match_side(m, "a", names))
            label(end + 43, cy - 3, match_side(m, "b", names))
        else:
            label(end + 43, cy + 13, names.get(m["winner"], "胜者待定"))
            label(end + 9, cy - 3, "胜者进入四强循环赛")
        label(end + 9, cy - 19, " / ".join(s[:-1] for s in m["next"]), font="TNR")
    if redraw:
        label(columns[2], 8, "5 支轮空队伍 + 3 场附加赛胜者，共 8 支队伍重新抽签")
    return chart


def pdf(title, subtitle, columns, lines, wide, brackets=()):
    if "Song" not in pdfmetrics.getRegisteredFontNames():
        fonts = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts"
        song = Path(os.environ.get("QINGYOU_SIMSUN", str(fonts / "simsun.ttc")))
        times = Path(os.environ.get("QINGYOU_TIMES", str(fonts / "times.ttf")))
        need(song.is_file() and times.is_file(), "PDF字体未配置，请设置宋体和 Times New Roman 文件路径")
        pdfmetrics.registerFont(TTFont("Song", str(song), subfontIndex=0))
        pdfmetrics.registerFont(TTFont("TNR", str(times)))
    page = landscape(A4) if wide else A4
    out = BytesIO()
    doc = SimpleDocTemplate(out, pagesize=page, leftMargin=28, rightMargin=28,
                            topMargin=31, bottomMargin=32, title=title)
    base = ParagraphStyle("cell", fontName="Song", fontSize=9, leading=13, wordWrap="CJK")
    heading = ParagraphStyle("heading", parent=base, fontSize=17, leading=23)

    def paragraph(value, style=base):
        return Paragraph("".join(f'<font name="TNR">{escape(part)}</font>' if i % 2 else escape(part)
                                 for i, part in enumerate(LATIN.split(str(value)))), style)

    rows = [[paragraph(x) for x in line] for line in [columns, *lines]]
    fractions = [1] * len(columns)
    if columns[0] == "开始（北京时间）":
        fractions = [2, 1.15, .55, .75, 3.1, 1.1]
    elif columns[0] == "编号":
        fractions = [.65, 1.3, 1.8, 1.8, .9, 2 if title == REPORTS["scores"] else 1, 1.5, 1.2]
    elif title == REPORTS["draw"]:
        fractions = [1, 2.3, 1, 1, 1, .9, 2.1] if len(columns) == 7 else [1.3, .65, .65, .65, 2.2, 2.1]
    elif title == REPORTS["standings"]:
        fractions = [1.4, 2.4, .7, .7, .7, .7, .7, .7]
    table = Table(rows, colWidths=[(doc.width - 10) * f / sum(fractions) for f in fractions], repeatRows=1)
    table.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), .45, colors.black),
                               ("VALIGN", (0, 0), (-1, -1), "TOP"),
                               ("TOPPADDING", (0, 0), (-1, -1), 6),
                               ("BOTTOMPADDING", (0, 0), (-1, -1), 6)]))
    flow = [paragraph(title, heading), Spacer(1, 7), paragraph(subtitle), Spacer(1, 12), table]
    if not lines:
        flow.extend([Spacer(1, 9), paragraph("暂无数据")])

    def footer(canvas, document):
        canvas.setFont("TNR", 9)
        canvas.drawRightString(page[0] - 28, 17, str(document.page))

    for state, project in brackets:
        flow.extend([PageBreak(), paragraph(PROJECTS[project] + " · 淘汰赛晋级图", heading), Spacer(1, 6),
                     paragraph(subtitle), Spacer(1, 12), bracket_diagram(state, project, doc.width - 12)])
    doc.build(flow, onFirstPage=footer, onLaterPages=footer)
    out.seek(0)
    return out


def make_export(state, kind, filetype):
    cols, lines = table_data(state, kind)
    title = REPORTS[kind]
    subtitle = state["title"] + " · " + state["venue"]
    if state["college_draw"]["preview"]:
        subtitle += " · 预演"
    wide = kind in PROJECTS or kind in ("scores", "matchups")
    need(filetype in ("xlsx", "pdf"), "仅支持 Excel 和 PDF")
    if filetype == "xlsx":
        return xlsx(title, subtitle, cols, lines, wide), title + ".xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    diagram_projects = PROJECTS if kind == "matchups" else [kind] if kind in PROJECTS else []
    brackets = [(state, p) for p in diagram_projects if state["projects"][p]["state"] == "locked"]
    return pdf(title, subtitle, cols, lines, wide, brackets), title + ".pdf", "application/pdf"
