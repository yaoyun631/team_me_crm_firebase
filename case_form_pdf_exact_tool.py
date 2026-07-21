# -*- coding: utf-8 -*-
"""
Team M.E｜案件輸入表 PDF 同版型精準填寫工具 v6

核心目標：
- 保留原本「案件輸入表-使用中.pdf」作為背景，不重新畫表格。
- 用相同大小的文字把資料填到空格中。
- 產生 PDF，因此版型會跟原始 PDF 一模一樣。

安裝：
    pip install pypdf reportlab

字體：
- Windows 本機會優先使用 C:\\Windows\\Fonts\\kaiu.ttf，也就是標楷體。
- 不會把字體檔包進專案，避免授權問題。
- Render / Linux 若沒有標楷體，會 fallback 到內建中文字型。

獨立測試：
    python case_form_pdf_exact_tool.py fill H2290000040070a.pdf assets/案件輸入表-使用中.pdf output.pdf
"""

from __future__ import annotations

from io import BytesIO
from pathlib import Path
from typing import Any
import argparse
import math
import os
import re

A4_W = 595.32
A4_H = 841.92


def clean(value: Any) -> str:
    if value is None:
        return ""
    try:
        if isinstance(value, float) and math.isnan(value):
            return ""
    except Exception:
        pass
    return str(value).strip()


def fmt_num(value: Any, digits: int = 2) -> str:
    text = clean(value)
    if not text:
        return ""
    try:
        num = round(float(text.replace(",", "")), digits)
        return f"{num:.{digits}f}".rstrip("0").rstrip(".")
    except Exception:
        return text


def pt_from_top(x: float, y_top: float, page_h: float = A4_H) -> tuple[float, float]:
    """PDF 座標是左下角原點；這裡讓你用比較直覺的左上角 y_top。"""
    return x, page_h - y_top


def register_case_font():
    """優先用標楷體。不能附字體檔，所以用本機字體路徑。"""
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont
    from reportlab.pdfbase.ttfonts import TTFont

    font_candidates = [
        os.environ.get("CASE_FORM_KAI_FONT", ""),
        r"C:\Windows\Fonts\kaiu.ttf",       # Windows 標楷體
        r"C:\Windows\Fonts\KAIU.TTF",
        r"C:\Windows\Fonts\DFKai-SB.ttf",
        "/System/Library/Fonts/STHeiti Medium.ttc",
        "/usr/share/fonts/truetype/arphic/ukai.ttc",
        "/usr/share/fonts/truetype/arphic/ukai.ttf",
    ]

    for fp in font_candidates:
        if not fp:
            continue
        try:
            if os.path.exists(fp):
                pdfmetrics.registerFont(TTFont("CaseKai", fp))
                return "CaseKai"
        except Exception:
            continue

    # fallback：不一定是標楷體，但可保證中文不亂碼。
    try:
        pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))
        return "STSong-Light"
    except Exception:
        return "Helvetica"


# 用原 PDF 當背景，所以只需要填資料位置。
# x, y_top, font_size, width, max_lines
TEXT_POS = {
    # 客戶資料
    "owner_name": (46, 122, 9.5, 90, 1),
    "owner_id": (185, 122, 9.5, 120, 1),
    "owner_mobile": (420, 151, 9.5, 120, 1),
    "owner_address": (60, 176, 9.0, 465, 1),

    # 1. 基本資料
    "property_title": (86, 215, 9.5, 210, 1),
    "community_name": (500, 215, 9.5, 72, 1),
    "case_address": (70, 297, 9.0, 480, 1),
    "floor_total": (91, 303, 9.5, 32, 1),
    "basement_total": (184, 303, 9.5, 28, 1),
    "floor": (96, 322, 9.5, 40, 1),
    "floor_end": (160, 322, 9.5, 40, 1),
    "layout": (236, 322, 9.5, 190, 1),
    "completed_year": (130, 340, 9.5, 24, 1),
    "completed_month": (174, 340, 9.5, 18, 1),
    "completed_day": (211, 340, 9.5, 18, 1),
    "building_age": (303, 340, 9.5, 26, 1),
    "facing": (512, 290, 9.5, 28, 1),

    # 2. 結構 / 車位
    "road_width": (96, 405, 9.0, 30, 1),
    "management_fee": (92, 442, 9.0, 54, 1),
    "elevator_count": (392, 442, 9.0, 26, 1),
    "households_per_floor": (516, 442, 9.0, 28, 1),
    "parking_no": (96, 513, 9.0, 70, 1),
    "motorcycle_no": (229, 513, 9.0, 72, 1),
    "parking_fee": (360, 513, 9.0, 52, 1),
    "parking_note": (488, 513, 8.5, 85, 1),

    # 3. 面積 / 金額
    "total_ping": (102, 579, 9.5, 44, 1),
    "main_ping": (213, 579, 9.5, 44, 1),
    "attached_ping": (320, 579, 9.5, 44, 1),
    "public_ping": (425, 579, 9.5, 44, 1),
    "parking_ping": (525, 579, 9.5, 42, 1),
    "land_ping": (102, 598, 9.5, 44, 1),
    "base_land_ping": (245, 598, 9.5, 44, 1),
    "land_share_ping": (415, 598, 9.5, 44, 1),
    "case_price": (68, 617, 9.5, 60, 1),
    "rent_price": (227, 617, 9.5, 58, 1),
    "deposit": (365, 617, 9.5, 55, 1),
    "deposit_months": (522, 617, 9.5, 45, 1),

    # 4. 學區 / 環境
    "elementary_school": (95, 647, 9.0, 75, 1),
    "junior_high_school": (260, 647, 9.0, 80, 1),
    "market": (459, 647, 9.0, 80, 1),
    "park": (95, 665, 9.0, 75, 1),
    "medical": (260, 665, 9.0, 80, 1),
    "station": (459, 665, 9.0, 80, 1),
    "builder": (95, 686, 9.0, 75, 1),
    "business_area": (260, 686, 9.0, 80, 1),

    # 5. 特色備註
    "feature_note": (134, 728, 7.4, 425, 2),
    "special_note": (134, 780, 6.8, 425, 2),
}

# 勾選框中心位置：x, y_top, size
CHECK_POS = {
    "deal_sale": (84, 51, 7),
    "deal_rent": (124, 51, 7),
    "mandate_exclusive": (264, 51, 7),
    "mandate_general": (314, 51, 7),
    "source_deed": (301, 98, 7),

    "type_apartment": (84, 232, 7),
    "type_huaxia": (143, 232, 7),
    "type_toutian": (191, 232, 7),
    "type_villa": (238, 232, 7),
    "type_farmhouse": (285, 232, 7),
    "type_store": (330, 232, 7),
    "type_suite": (374, 232, 7),
    "type_factory": (421, 232, 7),

    "status_empty": (84, 267, 7),
    "status_self_use": (134, 267, 7),
    "status_rented": (184, 267, 7),
    "status_structure": (234, 267, 7),
    "status_land": (290, 267, 7),
    "status_other": (338, 267, 7),

    "structure_brick": (82, 392, 7),
    "structure_reinforced_brick": (139, 392, 7),
    "structure_rc": (246, 392, 7),
    "structure_src": (359, 392, 7),
    "structure_stone": (454, 392, 7),
    "structure_other": (500, 392, 7),

    "use_residential": (132, 542, 7),
    "use_store": (190, 542, 7),
    "use_public_housing": (242, 542, 7),
    "use_parking": (302, 542, 7),
    "use_factory": (377, 542, 7),
    "use_commercial": (461, 542, 7),
    "use_office": (530, 542, 7),
    "use_res_mix": (132, 560, 7),
    "use_res_industry": (190, 560, 7),
    "use_other": (242, 560, 7),
}


def text_units(text: str) -> float:
    return sum(1.0 if ord(ch) > 127 else 0.55 for ch in text)


def wrap_text(text: str, width_pt: float, font_size: float, max_lines: int) -> list[str]:
    text = clean(text)
    if not text:
        return []
    # 中文約 0.95em，英數約 0.55em；這只是為了不要超出欄位。
    max_units = max(1, int(width_pt / max(font_size * 0.92, 1)))
    lines: list[str] = []
    for raw_line in text.splitlines():
        cur = ""
        for ch in raw_line:
            if text_units(cur + ch) > max_units:
                if cur:
                    lines.append(cur)
                cur = ch
            else:
                cur += ch
        if cur:
            lines.append(cur)
    return lines[:max_lines]


def draw_text(c, key: str, value: Any, page_h: float):
    if key not in TEXT_POS:
        return
    value = clean(value)
    if not value:
        return
    x, y_top, size, width, max_lines = TEXT_POS[key]
    px, py = pt_from_top(x, y_top, page_h)
    lines = wrap_text(value, width, size, max_lines)
    if not lines:
        return
    c.setFont(c._case_font_name, size)
    c.setFillColorRGB(0, 0, 0)
    leading = size + 2.0
    for i, line in enumerate(lines):
        c.drawString(px, py - i * leading, line)


def draw_check(c, key: str, page_h: float):
    if key not in CHECK_POS:
        return
    x, y_top, size = CHECK_POS[key]
    px, py = pt_from_top(x, y_top, page_h)
    c.setLineWidth(1.0)
    c.setStrokeColorRGB(0, 0, 0)
    c.line(px - size * 0.45, py, px - size * 0.12, py - size * 0.35)
    c.line(px - size * 0.12, py - size * 0.35, px + size * 0.48, py + size * 0.45)


def parse_minguo_parts(date_text: str) -> tuple[str, str, str]:
    text = clean(date_text).translate(str.maketrans({"０":"0","１":"1","２":"2","３":"3","４":"4","５":"5","６":"6","７":"7","８":"8","９":"9"}))
    m = re.search(r"民國\s*(\d{2,4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日", text)
    if m:
        return m.group(1), m.group(2), m.group(3)
    m = re.search(r"(\d{2,4})\s*[年/-]\s*(\d{1,2})\s*[月/-]\s*(\d{1,2})", text)
    if m:
        y = int(m.group(1))
        if y > 1911:
            y -= 1911
        return str(y), m.group(2), m.group(3)
    return "", "", ""


def build_fill_fields(case_data: dict[str, Any], seller: dict[str, Any] | None = None) -> dict[str, str]:
    seller = seller or {}
    fields: dict[str, str] = {}

    def put(key: str, *values: Any):
        for value in values:
            value = clean(value)
            if value:
                fields[key] = value
                return

    put("owner_name", seller.get("name"))
    put("owner_id", seller.get("id_no"), seller.get("identity_no"))
    put("owner_mobile", seller.get("phone"), seller.get("mobile"))
    put("owner_address", seller.get("contact_address"), seller.get("address"))

    put("property_title", case_data.get("property_title"), case_data.get("ai_sales_title"))
    put("community_name", case_data.get("community_name"))
    put("case_address", case_data.get("case_address"), seller.get("address"))
    put("floor_total", case_data.get("floor_total"))
    put("basement_total", case_data.get("basement_total"))
    put("floor", case_data.get("floor"))
    put("floor_end", case_data.get("floor_end"))
    put("layout", case_data.get("layout"))

    y = clean(case_data.get("completed_minguo_year"))
    m = clean(case_data.get("completed_month"))
    d = clean(case_data.get("completed_day"))
    if not (y and m and d):
        y, m, d = parse_minguo_parts(case_data.get("deed_completed_date") or "")
    put("completed_year", y)
    put("completed_month", m)
    put("completed_day", d)
    put("building_age", case_data.get("building_age"))
    put("facing", case_data.get("facing"))

    put("road_width", case_data.get("road_width"))
    put("management_fee", case_data.get("management_fee"))
    put("elevator_count", case_data.get("elevator_count"))
    put("households_per_floor", case_data.get("households_per_floor"))
    put("parking_no", case_data.get("parking_no"))
    put("motorcycle_no", case_data.get("motorcycle_no"))
    put("parking_fee", case_data.get("parking_fee"))
    put("parking_note", case_data.get("parking_note"))

    put("total_ping", fmt_num(case_data.get("total_ping")))
    put("main_ping", fmt_num(case_data.get("main_ping")))
    put("attached_ping", fmt_num(case_data.get("attached_ping")))
    put("public_ping", fmt_num(case_data.get("public_ping")))
    put("parking_ping", fmt_num(case_data.get("parking_ping")))
    put("land_ping", fmt_num(case_data.get("land_ping")))
    put("base_land_ping", fmt_num(case_data.get("base_land_ping")), fmt_num(case_data.get("land_ping")))
    put("land_share_ping", fmt_num(case_data.get("land_share_ping")), fmt_num(case_data.get("land_ping")))
    put("case_price", case_data.get("case_price"), seller.get("expected_price"))
    put("rent_price", case_data.get("rent_price"))
    put("deposit", case_data.get("deposit"))
    put("deposit_months", case_data.get("deposit_months"))

    for key in ["elementary_school", "junior_high_school", "market", "park", "medical", "station", "builder", "business_area"]:
        put(key, case_data.get(key))

    feature_parts = []
    for k in ["ai_feature_note", "property_highlight_note", "life_note", "target_customer_note"]:
        if clean(case_data.get(k)):
            feature_parts.append(clean(case_data.get(k)))
    if not feature_parts and clean(case_data.get("deed_parsed_note")):
        feature_parts.append(clean(case_data.get("deed_parsed_note"))[:180])
    put("feature_note", "\n".join(feature_parts))

    special_parts = []
    if clean(case_data.get("deed_mortgage_note")):
        special_parts.append(clean(case_data.get("deed_mortgage_note")))
    if clean(case_data.get("case_note")) and clean(case_data.get("case_note")) not in special_parts:
        special_parts.append(clean(case_data.get("case_note")))
    put("special_note", "\n".join(special_parts))

    return fields


def infer_checks(case_data: dict[str, Any], seller: dict[str, Any] | None = None) -> dict[str, bool]:
    seller = seller or {}
    checks: dict[str, bool] = {}
    deal = clean(seller.get("deal_type") or case_data.get("deal_type") or "sale").lower()
    checks["deal_rent"] = deal in {"rent", "出租", "租"}
    checks["deal_sale"] = not checks["deal_rent"]
    checks["source_deed"] = True

    ptype = clean(seller.get("property_type") or case_data.get("property_type"))
    main_use = clean(case_data.get("deed_main_use"))
    material = clean(case_data.get("deed_main_material"))
    floor_total = clean(case_data.get("floor_total"))

    if "透天" in ptype or (floor_total.isdigit() and int(floor_total) <= 5 and "住宅" in main_use):
        checks["type_toutian"] = True
    elif "華廈" in ptype or "華夏" in ptype:
        checks["type_huaxia"] = True
    elif "公寓" in ptype:
        checks["type_apartment"] = True
    elif "店" in ptype:
        checks["type_store"] = True
    elif "廠" in ptype or "工業" in main_use:
        checks["type_factory"] = True

    if "鋼筋混凝土" in material or "RC" in material.upper():
        checks["structure_rc"] = True
    elif "加強磚" in material:
        checks["structure_reinforced_brick"] = True
    elif "磚" in material:
        checks["structure_brick"] = True
    elif "鋼骨" in material or "SRC" in material.upper():
        checks["structure_src"] = True
    elif material:
        checks["structure_other"] = True

    if "住宅" in main_use or "住家" in main_use:
        checks["use_residential"] = True
    if "店" in main_use:
        checks["use_store"] = True
    if "停車" in main_use or "車庫" in main_use:
        checks["use_parking"] = True
    if "工業" in main_use or "廠" in main_use:
        checks["use_factory"] = True
    if "商業" in main_use:
        checks["use_commercial"] = True
    if "辦公" in main_use:
        checks["use_office"] = True

    return checks


def fill_case_form_exact_pdf_bytes(
    template_pdf: str | Path,
    case_data: dict[str, Any],
    seller: dict[str, Any] | None = None,
    extra_fields: dict[str, Any] | None = None,
    extra_checks: dict[str, bool] | None = None,
) -> bytes:
    """使用原始案件輸入表 PDF 當背景，產生同版型填寫後 PDF。"""
    try:
        from pypdf import PdfReader, PdfWriter
        from reportlab.pdfgen import canvas
    except Exception as e:
        raise RuntimeError("缺少套件，請先安裝：pip install pypdf reportlab") from e

    template_pdf = Path(template_pdf)
    if not template_pdf.exists():
        raise FileNotFoundError(f"找不到案件輸入表 PDF 範本：{template_pdf}")

    seller = seller or {}
    fields = build_fill_fields(case_data, seller)
    if extra_fields:
        for k, v in extra_fields.items():
            if clean(v):
                fields[k] = clean(v)

    checks = infer_checks(case_data, seller)
    if extra_checks:
        checks.update(extra_checks)

    reader = PdfReader(str(template_pdf))
    bg_page = reader.pages[0]
    page_w = float(bg_page.mediabox.width)
    page_h = float(bg_page.mediabox.height)

    packet = BytesIO()
    c = canvas.Canvas(packet, pagesize=(page_w, page_h))
    c._case_font_name = register_case_font()

    for key, value in fields.items():
        draw_text(c, key, value, page_h)

    for key, enabled in checks.items():
        if enabled:
            draw_check(c, key, page_h)

    c.save()
    packet.seek(0)

    overlay = PdfReader(packet)
    bg_page.merge_page(overlay.pages[0])

    writer = PdfWriter()
    writer.add_page(bg_page)
    out = BytesIO()
    writer.write(out)
    return out.getvalue()


def fill_case_form_exact_pdf_file(template_pdf: str | Path, output_pdf: str | Path, case_data: dict[str, Any], seller: dict[str, Any] | None = None):
    output_pdf = Path(output_pdf)
    output_pdf.parent.mkdir(parents=True, exist_ok=True)
    output_pdf.write_bytes(fill_case_form_exact_pdf_bytes(template_pdf, case_data, seller=seller))
    return output_pdf


def main(argv=None):
    parser = argparse.ArgumentParser(description="案件輸入表 PDF 同版型精準填寫")
    parser.add_argument("deed_pdf", help="謄本 PDF")
    parser.add_argument("template_pdf", help="案件輸入表-使用中.pdf")
    parser.add_argument("output_pdf", help="輸出 PDF")
    parser.add_argument("--seller-name", default="")
    parser.add_argument("--seller-phone", default="")
    parser.add_argument("--price", default="")
    parser.add_argument("--property-type", default="透天")
    args = parser.parse_args(argv)

    from deed_case_form_tool import parse_pdf_to_case_data
    parsed = parse_pdf_to_case_data(args.deed_pdf)
    case_data = parsed.get("case_data") or {}
    if args.price:
        case_data["case_price"] = args.price
    seller = {
        "name": args.seller_name,
        "phone": args.seller_phone,
        "expected_price": args.price,
        "property_type": args.property_type,
        "deal_type": "sale",
    }
    fill_case_form_exact_pdf_file(args.template_pdf, args.output_pdf, case_data, seller=seller)
    print(f"已產生：{args.output_pdf}")


if __name__ == "__main__":
    main()
