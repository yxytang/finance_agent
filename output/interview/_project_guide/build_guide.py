"""Build the interview guide with native Word text, headings and tables."""
from pathlib import Path
import re

from docx import Document
from docx.enum.table import WD_CELL_VERTICAL_ALIGNMENT, WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Inches, Pt, RGBColor

ROOT = Path(__file__).resolve().parent
SOURCE = ROOT / "项目讲解正文.md"
OUTPUT = ROOT.parent / "个人财务分析系统_逐条技术讲解与面试准备.docx"


def font(style, size, bold=False):
    style.font.name = "Calibri"
    style.font.size = Pt(size)
    style.font.bold = bold
    style.font.color.rgb = RGBColor(0, 0, 0)
    style.element.get_or_add_rPr().rFonts.set(qn("w:eastAsia"), "微软雅黑")


def rich(paragraph, text):
    for i, part in enumerate(re.split(r"\*\*(.*?)\*\*", text)):
        if part:
            run = paragraph.add_run(part)
            run.bold = bool(i % 2)


def field(paragraph, name):
    run = paragraph.add_run()
    begin = OxmlElement("w:fldChar")
    begin.set(qn("w:fldCharType"), "begin")
    code = OxmlElement("w:instrText")
    code.set(qn("xml:space"), "preserve")
    code.text = " " + name + " "
    separate = OxmlElement("w:fldChar")
    separate.set(qn("w:fldCharType"), "separate")
    value = OxmlElement("w:t")
    value.text = "1"
    end = OxmlElement("w:fldChar")
    end.set(qn("w:fldCharType"), "end")
    for item in (begin, code, separate, value, end):
        run._r.append(item)


def table(doc, rows):
    rows = [row for row in rows if not all(re.fullmatch(r":?-+:?", c.strip()) for c in row)]
    count = len(rows[0])
    widths = {2: [1.64, 5.36], 3: [2.0, 0.75, 4.25], 4: [2.55, 1.8, 1.1, 1.55]}[count]
    t = doc.add_table(rows=0, cols=count)
    t.alignment = WD_TABLE_ALIGNMENT.CENTER
    t.autofit = False
    for col, width in zip(t.columns, widths):
        col.width = Inches(width)
    borders = OxmlElement("w:tblBorders")
    for edge in ("top", "left", "bottom", "right", "insideH", "insideV"):
        border = OxmlElement("w:" + edge)
        for name, val in (("val", "single"), ("sz", "5"), ("color", "D9D9D9")):
            border.set(qn("w:" + name), val)
        borders.append(border)
    t._tbl.tblPr.append(borders)
    for index, values in enumerate(rows):
        row = t.add_row()
        trpr = row._tr.get_or_add_trPr()
        cant = OxmlElement("w:cantSplit")
        trpr.append(cant)
        if index == 0:
            repeat = OxmlElement("w:tblHeader")
            trpr.append(repeat)
        for col, (cell, content) in enumerate(zip(row.cells, values)):
            cell.width = Inches(widths[col])
            cell.vertical_alignment = WD_CELL_VERTICAL_ALIGNMENT.CENTER
            tcpr = cell._tc.get_or_add_tcPr()
            margin = OxmlElement("w:tcMar")
            for edge, value in (("top", "85"), ("bottom", "85"), ("left", "110"), ("right", "110")):
                e = OxmlElement("w:" + edge)
                e.set(qn("w:w"), value)
                e.set(qn("w:type"), "dxa")
                margin.append(e)
            tcpr.append(margin)
            shade = OxmlElement("w:shd")
            shade.set(qn("w:fill"), "E5ECF2" if index == 0 else ("F7F9FB" if index % 2 == 0 else "FFFFFF"))
            tcpr.append(shade)
            p = cell.paragraphs[0]
            p.style = doc.styles["Table Text"]
            p.paragraph_format.space_after = Pt(0)
            p.paragraph_format.space_before = Pt(0)
            p.paragraph_format.line_spacing = 1.18
            if count == 4 and col > 0 or count == 3 and col == 1:
                p.alignment = WD_ALIGN_PARAGRAPH.CENTER
            rich(p, content)
            if index == 0:
                for run in p.runs:
                    run.bold = True
    after = doc.add_paragraph()
    after.paragraph_format.space_after = Pt(2)
    after.paragraph_format.space_before = Pt(0)
    after.paragraph_format.line_spacing = 0.3
    after.add_run().font.size = Pt(3)


def main():
    doc = Document()
    sec = doc.sections[0]
    sec.page_width, sec.page_height = Inches(8.5), Inches(11)
    sec.top_margin, sec.bottom_margin = Inches(0.68), Inches(0.68)
    sec.left_margin, sec.right_margin = Inches(0.75), Inches(0.75)
    sec.header_distance, sec.footer_distance = Inches(0.28), Inches(0.3)

    normal = doc.styles["Normal"]
    font(normal, 11)
    normal.paragraph_format.line_spacing = 1.25
    normal.paragraph_format.space_after = Pt(6)
    normal.paragraph_format.widow_control = True
    for name, size in (("Title", 21), ("Subtitle", 11), ("Heading 1", 15), ("Heading 2", 12)):
        s = doc.styles[name]
        font(s, size, name != "Subtitle")
        s.paragraph_format.keep_with_next = True
        s.paragraph_format.space_before = Pt(13 if "Heading" in name else 0)
        s.paragraph_format.space_after = Pt(7)
        s.paragraph_format.line_spacing = 1.12
        # Avoid theme paragraph borders, including Word's decorative title rule.
        ppr = s.element.find(qn("w:pPr"))
        if ppr is not None:
            for el in list(ppr):
                if el.tag == qn("w:pBdr"):
                    ppr.remove(el)
    font(doc.styles["Header"], 9)
    font(doc.styles["Footer"], 9)
    table_style = doc.styles.add_style("Table Text", 1)
    font(table_style, 10)
    sec.header.paragraphs[0].text = "个人财务分析系统 项目讲解与面试准备"
    footer = sec.footer.paragraphs[0]
    footer.alignment = WD_ALIGN_PARAGRAPH.RIGHT
    footer.add_run("第 ")
    field(footer, "PAGE")
    footer.add_run(" 页")

    props = doc.core_properties
    props.title = "个人财务分析系统项目讲解"
    props.subject = "按简历简介与五项技术点展开的问题设计实现验证与边界"
    props.author = "袁昕宇"
    props.keywords = "Agent Runtime RAG OCR SSE 面试 财务分析"

    lines = SOURCE.read_text(encoding="utf-8").splitlines()
    i = 0
    front = 0
    while i < len(lines):
        text = lines[i].strip()
        i += 1
        if not text:
            continue
        if text.startswith("| "):
            records = []
            while True:
                records.append([c.strip() for c in text.strip("|").split("|")])
                if i >= len(lines) or not lines[i].strip().startswith("| "):
                    break
                text = lines[i].strip()
                i += 1
            table(doc, records)
        elif text.startswith("# "):
            doc.add_paragraph(text[2:], style="Title")
        elif text.startswith("## "):
            doc.add_paragraph(text[3:], style="Heading 1")
        elif text.startswith("### "):
            doc.add_paragraph(text[4:], style="Heading 2")
        else:
            style = "Subtitle" if front < 2 else "Normal"
            p = doc.add_paragraph(style=style)
            rich(p, text)
            front += 1
    settings = doc.settings.element
    update = OxmlElement("w:updateFields")
    update.set(qn("w:val"), "true")
    settings.append(update)
    doc.save(OUTPUT)
    print(f"Created {OUTPUT.name}")
    print(f"Source characters {len(SOURCE.read_text(encoding='utf-8'))}; paragraphs {len(doc.paragraphs)}; tables {len(doc.tables)}")


if __name__ == "__main__":
    main()
