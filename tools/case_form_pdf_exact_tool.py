# -*- coding: utf-8 -*-
"""
Team M.E｜案件輸入表 PDF 同版型精準填寫工具 v16 - 性別戶籍身份完整填入版

核心目標：
- 保留原本「案件輸入表-使用中.pdf」作為背景，不重新畫表格。
- 用標楷體、置中、接近原表格字級把資料填進空格中，並修正朝向、身份字號、性別、生日與勾選位置。
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


CASE_FORM_NORMAL_SIZE = float(os.environ.get("CASE_FORM_NORMAL_SIZE", "10.8") or 10.8)
CASE_FORM_NUM_SIZE = float(os.environ.get("CASE_FORM_NUM_SIZE", "10.8") or 10.8)
CASE_FORM_NOTE_SIZE = float(os.environ.get("CASE_FORM_NOTE_SIZE", "9.8") or 9.8)


def parse_tw_address(value: Any) -> dict[str, str]:
    """把台灣地址拆成表格上的市/區/路/段/巷/弄/號/樓。

    目的不是完整地址標準化，而是避免把整串地址塞進一條空格造成重疊。
    """
    text = clean(value)
    text = text.translate(str.maketrans({
        "臺": "台",
        "０": "0", "１": "1", "２": "2", "３": "3", "４": "4",
        "５": "5", "６": "6", "７": "7", "８": "8", "９": "9",
        "－": "-", "—": "-",
    }))
    text = re.sub(r"\s+", "", text)

    result = {
        "city": "", "district": "", "road": "", "section": "",
        "lane": "", "alley": "", "no": "", "floor": "", "floor_extra": "",
        "raw": text,
    }

    if not text:
        return result

    m = re.match(r"^(.*?[縣市])", text)
    if m:
        result["city"] = m.group(1)
        text = text[len(m.group(1)):]

    m = re.match(r"^(.*?(?:區|鄉|鎮|市))", text)
    if m:
        result["district"] = m.group(1)
        text = text[len(m.group(1)):]

    # 路街大道等
    m = re.match(r"^(.*?(?:大道|路|街|巷|弄))", text)
    if m:
        road_part = m.group(1)
        # 如果直接遇到巷/弄，路名留空，後面再抓。
        if road_part.endswith(("路", "街", "大道")):
            result["road"] = re.sub(r"(大道|路|街)$", "", road_part)
            text = text[len(road_part):]

    m = re.match(r"^(.+?)段", text)
    if m:
        result["section"] = m.group(1)
        text = text[len(m.group(0)):]

    m = re.match(r"^(\d+(?:-\d+)?)巷", text)
    if m:
        result["lane"] = m.group(1)
        text = text[len(m.group(0)):]

    m = re.match(r"^(\d+(?:-\d+)?)弄", text)
    if m:
        result["alley"] = m.group(1)
        text = text[len(m.group(0)):]

    m = re.match(r"^(\d+(?:-\d+)?(?:之\d+)?)號?", text)
    if m:
        result["no"] = m.group(1)
        # 有「號」就一起吃掉；沒有號也只吃數字。
        consume = m.group(0)
        text = text[len(consume):]

    m = re.search(r"(\d+(?:-\d+)?)樓(?:之(\d+))?", text)
    if m:
        result["floor"] = m.group(1)
        result["floor_extra"] = m.group(2) or ""

    return result


def fill_address_components(fields: dict[str, str], prefix: str, address: Any):
    parsed = parse_tw_address(address)
    if not parsed.get("raw"):
        return

    # 有成功拆出縣市/行政區/路名，就用分欄位填；這樣一定不會蓋到原本表格文字。
    if parsed.get("city") or parsed.get("district") or parsed.get("road"):
        mapping = {
            "city": "city",
            "district": "district",
            "road": "road",
            "section": "section",
            "lane": "lane",
            "alley": "alley",
            "no": "no",
            "floor": "floor",
        }
        for src, dst in mapping.items():
            value = parsed.get(src, "")
            if value:
                # 表格本身已經印好「市、區、路(街)、段、巷、弄、號、樓之」，
                # 所以填進空格的資料要去掉這些單位字，避免變成「台中市 市」「大雅區 區」。
                if src == "city":
                    value = re.sub(r"[縣市]$", "", value)
                elif src == "district":
                    value = re.sub(r"[區鄉鎮市]$", "", value)
                fields[f"{prefix}_{dst}"] = value
        if parsed.get("floor_extra"):
            fields[f"{prefix}_floor_extra"] = parsed["floor_extra"]
        return

    # 解析失敗時才放完整地址，但用很小字、指定短寬，避免壓爆整列。
    if len(parsed["raw"]) <= 22:
        fields[f"{prefix}_full"] = parsed["raw"]


def normalize_price_text(value: Any) -> str:
    text = clean(value)
    if not text:
        return ""
    text = text.replace(",", "")
    m = re.search(r"\d+(?:\.\d+)?", text)
    return m.group(0) if m else text.replace("萬", "").strip()


def normalize_id_text(value: Any) -> str:
    text = clean(value).upper().replace(" ", "")
    text = re.sub(r"[^A-Z0-9*]", "", text)
    return text[:12]


def infer_gender_from_id(owner_id: Any) -> str:
    text = normalize_id_text(owner_id)
    if len(text) >= 2 and text[1] in {"1", "2"}:
        return "男" if text[1] == "1" else "女"
    return ""


def parse_owner_birth_parts(value: Any) -> tuple[str, str, str]:
    text = clean(value)
    if not text:
        return "", "", ""
    text = text.translate(str.maketrans({
        "０":"0","１":"1","２":"2","３":"3","４":"4",
        "５":"5","６":"6","７":"7","８":"8","９":"9",
    }))
    m = re.search(r"民國\s*(\d{2,3})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日", text)
    if m:
        return m.group(1), m.group(2), m.group(3)
    m = re.search(r"(\d{2,4})[/-](\d{1,2})[/-](\d{1,2})", text)
    if m:
        y = int(m.group(1))
        if y > 1911:
            y -= 1911
        return str(y), m.group(2), m.group(3)
    m = re.search(r"(\d{2,4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})", text)
    if m:
        y = int(m.group(1))
        if y > 1911:
            y -= 1911
        return str(y), m.group(2), m.group(3)
    return "", "", ""


def extract_owner_info_from_case_data(case_data: dict[str, Any]) -> dict[str, str]:
    """下載 PDF 時最後補抓：從 deed_parsed_json / deed_raw_text 補身份、性別、生日、戶籍地址。"""
    info: dict[str, str] = {}

    raw_json = clean(case_data.get("deed_parsed_json"))
    if raw_json:
        try:
            import json
            obj = json.loads(raw_json)
            if isinstance(obj, dict):
                for src in [obj.get("owner_info"), obj.get("case_data")]:
                    if isinstance(src, dict):
                        for k in [
                            "owner_name", "owner_id", "owner_identity_no", "identity_no",
                            "owner_gender", "gender", "owner_birth_date", "birth_date",
                            "owner_birth_year", "owner_birth_month", "owner_birth_day",
                            "owner_household_address", "registered_address", "戶籍地址",
                        ]:
                            if clean(src.get(k)) and not info.get(k):
                                info[k] = clean(src.get(k))
        except Exception:
            pass

    raw_text = clean(case_data.get("deed_raw_text"))
    if raw_text:
        try:
            from deed_case_form_tool import parse_owner_personal_info
            parsed = parse_owner_personal_info(raw_text)
            if isinstance(parsed, dict):
                for k, v in parsed.items():
                    if clean(v) and not info.get(k):
                        info[k] = clean(v)
        except Exception:
            pass

    return info


def parse_layout_counts(value: Any) -> dict[str, str]:
    text = clean(value).replace(" ", "")
    out = {"rooms": "", "halls": "", "baths": "", "balconies": "", "kitchens": ""}
    if not text:
        return out
    patterns = [
        ("rooms", r"(\d+)\s*房"),
        ("halls", r"(\d+)\s*(?:廳|厅|廰)"),
        ("baths", r"(\d+)\s*(?:衛|卫)"),
        ("balconies", r"(\d+)\s*陽台"),
        ("kitchens", r"(\d+)\s*廚房"),
    ]
    for key, pat in patterns:
        m = re.search(pat, text)
        if m:
            out[key] = m.group(1)
    if not any(out.values()):
        nums = re.findall(r"\d+", text)
        if len(nums) >= 1:
            out["rooms"] = nums[0]
        if len(nums) >= 2:
            out["halls"] = nums[1]
        if len(nums) >= 3:
            out["baths"] = nums[2]
    return out


def pt_from_top(x: float, y_top: float, page_h: float = A4_H) -> tuple[float, float]:
    """PDF 座標是左下角原點；這裡讓你用比較直覺的左上角 y_top。"""
    return x, page_h - y_top


def register_case_font():
    """優先使用標楷體 KaiU。

    注意：
    - 程式不附字體檔。
    - Windows 本機請使用 C:\\Windows\\Fonts\\kaiu.ttf。
    - Render / Linux 若沒有標楷體檔，可用環境變數 CASE_FORM_KAI_FONT 指到已安裝字體。
    """
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont
    from reportlab.pdfbase.ttfonts import TTFont

    font_candidates = [
        os.environ.get("CASE_FORM_KAI_FONT", ""),
        r"C:\Windows\Fonts\kaiu.ttf",
        r"C:\Windows\Fonts\KAIU.TTF",
        r"C:\Windows\Fonts\DFKai-SB.ttf",
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
        except Exception as exc:
            print("⚠️ 標楷體載入失敗：", fp, exc)
            continue

    # fallback：Linux 沒有標楷體時才使用，避免中文亂碼。
    # 若要 100% 標楷體，請在環境變數 CASE_FORM_KAI_FONT 指定 kaiu.ttf 的實際路徑。
    try:
        pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))
        return "STSong-Light"
    except Exception:
        return "Helvetica"


# 用原 PDF 當背景，所以只需要填資料位置。
# x, y_top, font_size, width, max_lines
TEXT_POS = {
    "owner_name": (56, 124.0, CASE_FORM_NORMAL_SIZE, 68, 1),
    "owner_id": (188, 124.0, CASE_FORM_NORMAL_SIZE, 76, 1),
    "owner_birth_year": (438, 124.0, CASE_FORM_NORMAL_SIZE, 22, 1),
    "owner_birth_month": (474, 124.0, CASE_FORM_NORMAL_SIZE, 22, 1),
    "owner_birth_day": (510, 124.0, CASE_FORM_NORMAL_SIZE, 18, 1),
    "owner_home_phone": (80, 151.0, CASE_FORM_NORMAL_SIZE, 88, 1),
    "owner_company_phone": (245, 151.0, CASE_FORM_NORMAL_SIZE, 105, 1),
    "owner_mobile": (423, 151.0, CASE_FORM_NORMAL_SIZE, 110, 1),

    "owner_city": (58, 176.0, CASE_FORM_NORMAL_SIZE, 44, 1),
    "owner_district": (119, 176.0, CASE_FORM_NORMAL_SIZE, 48, 1),
    "owner_road": (183, 176.0, CASE_FORM_NORMAL_SIZE, 40, 1),
    "owner_section": (262, 176.0, CASE_FORM_NORMAL_SIZE, 42, 1),
    "owner_lane": (322, 176.0, CASE_FORM_NORMAL_SIZE, 32, 1),
    "owner_alley": (370, 176.0, CASE_FORM_NORMAL_SIZE, 32, 1),
    "owner_no": (418, 176.0, CASE_FORM_NORMAL_SIZE, 28, 1),
    "owner_floor": (459, 176.0, CASE_FORM_NORMAL_SIZE, 26, 1),
    "owner_floor_extra": (512, 176.0, CASE_FORM_NORMAL_SIZE, 25, 1),
    "owner_full": (58, 176.0, CASE_FORM_NORMAL_SIZE, 450, 1),

    "property_title": (87, 216.0, CASE_FORM_NORMAL_SIZE, 200, 1),
    "community_name": (490, 216.0, CASE_FORM_NORMAL_SIZE, 68, 1),
    "sunlight": (501, 247.0, CASE_FORM_NORMAL_SIZE, 28, 1),
    "front_width": (399, 263.0, CASE_FORM_NUM_SIZE, 28, 1),
    "depth": (506, 263.0, CASE_FORM_NUM_SIZE, 28, 1),
    "land_location_note": (459, 279.0, 9.4, 110, 1),

    "case_city": (84, 298.0, CASE_FORM_NORMAL_SIZE, 42, 1),
    "case_district": (142, 298.0, CASE_FORM_NORMAL_SIZE, 48, 1),
    "case_road": (208, 298.0, CASE_FORM_NORMAL_SIZE, 48, 1),
    "case_section": (297, 298.0, CASE_FORM_NORMAL_SIZE, 43, 1),
    "case_lane": (358, 298.0, CASE_FORM_NORMAL_SIZE, 37, 1),
    "case_alley": (412, 298.0, CASE_FORM_NORMAL_SIZE, 31, 1),
    "case_no": (460, 298.0, CASE_FORM_NORMAL_SIZE, 31, 1),
    "case_floor": (508, 298.0, CASE_FORM_NORMAL_SIZE, 25, 1),
    "case_floor_extra": (562, 298.0, CASE_FORM_NORMAL_SIZE, 20, 1),
    "case_full": (84, 298.0, CASE_FORM_NORMAL_SIZE, 475, 1),

    "floor_total": (88, 314.0, CASE_FORM_NUM_SIZE, 16, 1),
    "basement_total": (186, 314.0, CASE_FORM_NUM_SIZE, 14, 1),
    "facing": (309, 314.0, CASE_FORM_NORMAL_SIZE, 16, 1),
    "building_facing": (309, 314.0, CASE_FORM_NORMAL_SIZE, 16, 1),
    "tower_facing": (405, 314.0, CASE_FORM_NORMAL_SIZE, 16, 1),
    "balcony_facing": (495, 314.0, CASE_FORM_NORMAL_SIZE, 16, 1),

    "floor": (86, 329.0, CASE_FORM_NUM_SIZE, 18, 1),
    "floor_end": (104, 329.0, CASE_FORM_NUM_SIZE, 18, 1),
    "layout_rooms": (236, 329.0, CASE_FORM_NUM_SIZE, 12, 1),
    "layout_halls": (284, 329.0, CASE_FORM_NUM_SIZE, 12, 1),
    "layout_baths": (332, 329.0, CASE_FORM_NUM_SIZE, 12, 1),
    "layout_balconies": (387, 329.0, CASE_FORM_NUM_SIZE, 12, 1),
    "layout_kitchens": (447, 329.0, CASE_FORM_NUM_SIZE, 12, 1),
    "completed_year": (116, 343.0, CASE_FORM_NUM_SIZE, 12, 1),
    "completed_month": (164, 343.0, CASE_FORM_NUM_SIZE, 12, 1),
    "completed_day": (212, 343.0, CASE_FORM_NUM_SIZE, 12, 1),
    "building_age": (296, 343.0, CASE_FORM_NUM_SIZE, 12, 1),

    "road_width": (86, 405.0, CASE_FORM_NUM_SIZE, 28, 1),
    "management_fee": (86, 435.0, CASE_FORM_NUM_SIZE, 36, 1),
    "clean_fee": (298, 435.0, CASE_FORM_NUM_SIZE, 36, 1),
    "elevator_count": (424, 435.0, CASE_FORM_NUM_SIZE, 12, 1),
    "households_per_floor": (526, 435.0, CASE_FORM_NUM_SIZE, 18, 1),
    "parking_no": (81, 511.0, CASE_FORM_NORMAL_SIZE, 42, 1),
    "motorcycle_no": (191, 511.0, CASE_FORM_NORMAL_SIZE, 48, 1),
    "parking_fee": (308, 511.0, CASE_FORM_NUM_SIZE, 35, 1),
    "parking_note": (454, 511.0, CASE_FORM_NORMAL_SIZE, 110, 1),

    "total_ping": (98, 583.0, CASE_FORM_NUM_SIZE, 36, 1),
    "main_ping": (212, 583.0, CASE_FORM_NUM_SIZE, 30, 1),
    "attached_ping": (314, 583.0, CASE_FORM_NUM_SIZE, 24, 1),
    "public_ping": (410, 583.0, CASE_FORM_NUM_SIZE, 24, 1),
    "parking_ping": (506, 583.0, CASE_FORM_NUM_SIZE, 24, 1),
    "land_ping": (98, 601.0, CASE_FORM_NUM_SIZE, 36, 1),
    "base_land_ping": (242, 601.0, CASE_FORM_NUM_SIZE, 36, 1),
    "land_share_ping": (380, 601.0, CASE_FORM_NUM_SIZE, 36, 1),
    "case_price": (62, 619.0, CASE_FORM_NUM_SIZE, 36, 1),
    "rent_price": (206, 619.0, CASE_FORM_NUM_SIZE, 36, 1),
    "deposit": (350, 619.0, CASE_FORM_NUM_SIZE, 36, 1),
    "deposit_months": (500, 619.0, CASE_FORM_NUM_SIZE, 36, 1),

    "elementary_school": (81, 652.0, CASE_FORM_NORMAL_SIZE, 88, 1),
    "junior_high_school": (252, 652.0, CASE_FORM_NORMAL_SIZE, 88, 1),
    "market": (422, 652.0, CASE_FORM_NORMAL_SIZE, 88, 1),
    "park": (81, 669.0, CASE_FORM_NORMAL_SIZE, 88, 1),
    "medical": (252, 669.0, CASE_FORM_NORMAL_SIZE, 88, 1),
    "station": (422, 669.0, CASE_FORM_NORMAL_SIZE, 88, 1),
    "builder": (81, 686.0, CASE_FORM_NORMAL_SIZE, 88, 1),
    "business_area": (252, 686.0, CASE_FORM_NORMAL_SIZE, 88, 1),
    "feature_note": (130, 729.0, CASE_FORM_NOTE_SIZE, 440, 2),
    "special_note": (130, 782.0, CASE_FORM_NOTE_SIZE, 440, 2),
}
# 勾選框中心位置：x, y_top, size
CHECK_POS = {
    "deal_sale": (86.4, 51.9, 4.7),
    "deal_rent": (122.4, 51.9, 4.7),
    "mandate_exclusive": (236.4, 51.9, 4.7),
    "mandate_general": (272.4, 51.9, 4.7),

    "show_agent": (86.4, 69.6, 4.7),
    "show_store_1": (170.4, 69.6, 4.7),
    "show_store_2": (203.4, 69.6, 4.7),
    "show_store_3": (236.4, 69.6, 4.7),
    "show_store_4": (269.4, 69.6, 4.7),
    "show_store_5": (302.4, 69.6, 4.7),
    "show_store_6": (335.5, 69.6, 4.7),
    "show_store_7": (368.5, 69.6, 4.7),
    "show_management": (401.5, 69.6, 4.7),
    "show_other": (455.5, 69.6, 4.7),

    "source_line": (86.4, 101.9, 4.7),
    "source_store": (134.4, 101.9, 4.7),
    "source_call": (182.4, 101.9, 4.7),
    "source_deed": (230.4, 101.9, 4.7),
    "source_friend": (296.4, 101.9, 4.7),
    "source_other": (344.5, 101.9, 4.7),

    "gender_male": (310.8, 120.1, 4.5),
    "gender_female": (335.4, 120.1, 4.5),

    "type_apartment": (86.4, 229.8, 4.7),
    "type_huaxia": (122.4, 229.8, 4.7),
    "type_toutian": (158.4, 229.8, 4.7),
    "type_villa": (194.4, 229.8, 4.7),
    "type_farmhouse": (230.4, 229.8, 4.7),
    "type_store": (266.4, 229.8, 4.7),
    "type_suite": (302.4, 229.8, 4.7),
    "type_factory": (338.4, 229.8, 4.7),

    "land_building": (86.4, 245.4, 4.7),
    "land_commercial": (122.4, 245.4, 4.7),
    "land_industrial": (182.4, 245.4, 4.7),
    "land_agricultural": (242.4, 245.4, 4.7),
    "land_protected": (302.4, 245.4, 4.7),
    "land_other": (350.5, 245.4, 4.7),

    "status_empty": (86.4, 261.0, 4.7),
    "status_self_use": (122.4, 261.0, 4.7),
    "status_rented": (158.4, 261.0, 4.7),
    "status_structure": (194.4, 261.0, 4.7),
    "status_land": (242.4, 261.0, 4.7),
    "status_other": (278.4, 261.0, 4.7),

    "reason_change_house": (86.4, 276.6, 4.7),
    "reason_work": (122.4, 276.6, 4.7),
    "reason_school": (158.4, 276.6, 4.7),
    "reason_immigrate": (194.4, 276.6, 4.7),
    "reason_cash": (230.4, 276.6, 4.7),
    "reason_other": (290.4, 276.6, 4.7),

    "structure_brick": (86.4, 387.0, 4.7),
    "structure_reinforced_brick": (134.4, 387.0, 4.7),
    "structure_rc": (206.4, 387.0, 4.7),
    "structure_src": (305.4, 387.0, 4.7),
    "structure_stone": (434.5, 387.0, 4.7),
    "structure_other": (482.5, 387.0, 4.7),

    "use_residential": (134.4, 544.3, 4.7),
    "use_store": (188.4, 544.3, 4.7),
    "use_public_housing": (230.4, 544.3, 4.7),
    "use_parking": (296.4, 544.3, 4.7),
    "use_factory": (362.5, 544.3, 4.7),
    "use_commercial": (452.5, 544.3, 4.7),
    "use_office": (506.5, 544.3, 4.7),
    "use_res_mix": (134.4, 562.3, 4.7),
    "use_res_industry": (188.4, 562.3, 4.7),
    "use_other": (242.4, 562.3, 4.7),
}

def pdf_text_width(text: str, font_name: str, font_size: float) -> float:
    try:
        from reportlab.pdfbase.pdfmetrics import stringWidth
        return stringWidth(text, font_name, font_size)
    except Exception:
        return sum((font_size if ord(ch) > 127 else font_size * 0.55) for ch in text)


def truncate_to_width(text: str, font_name: str, font_size: float, width: float) -> str:
    text = clean(text)
    if not text:
        return ""
    if pdf_text_width(text, font_name, font_size) <= width:
        return text
    ell = "…"
    result = ""
    for ch in text:
        if pdf_text_width(result + ch + ell, font_name, font_size) > width:
            break
        result += ch
    return (result + ell) if result else ""


def wrap_to_lines(text: str, font_name: str, font_size: float, width: float, max_lines: int) -> list[str]:
    text = re.sub(r"\s+", " ", clean(text).replace("\n", " ").replace("\r", " ")).strip()
    if not text:
        return []
    lines = []
    current = ""
    consumed = 0
    for ch in text:
        candidate = current + ch
        if current and pdf_text_width(candidate, font_name, font_size) > width:
            lines.append(current)
            consumed += len(current)
            current = ch
            if len(lines) >= max_lines:
                break
        else:
            current = candidate
    if len(lines) < max_lines and current:
        lines.append(current)
        consumed += len(current)
    if lines and consumed < len(text):
        lines[-1] = truncate_to_width(lines[-1] + "…", font_name, font_size, width)
    return lines[:max_lines]


def draw_centered_fit_text(c, line: str, x: float, y: float, width: float, font_name: str, size: float, min_size: float):
    line = clean(line)
    if not line:
        return
    draw_size = float(size)
    while draw_size > min_size and pdf_text_width(line, font_name, draw_size) > width:
        draw_size -= 0.15
    if pdf_text_width(line, font_name, draw_size) > width:
        line = truncate_to_width(line, font_name, draw_size, width)
    if not line:
        return
    c.setFont(font_name, draw_size)
    c.setFillColorRGB(0, 0, 0)
    text_w = pdf_text_width(line, font_name, draw_size)
    draw_x = x + max(0, (width - text_w) / 2)
    c.drawString(draw_x, y, line)


def draw_text(c, key: str, value: Any, page_h: float):
    if key not in TEXT_POS:
        return
    value = clean(value)
    if not value:
        return
    x, y_top, size, width, max_lines = TEXT_POS[key]
    px, py = pt_from_top(x, y_top, page_h)
    font_name = c._case_font_name
    line = re.sub(r"\s+", " ", value.replace("\n", " ").replace("\r", " ")).strip()

    if key in {"feature_note", "special_note"}:
        lines = wrap_to_lines(line, font_name, float(size), width, int(max_lines or 1))
        c.setFont(font_name, float(size))
        c.setFillColorRGB(0, 0, 0)
        leading = float(size) + 1.0
        for i, part in enumerate(lines):
            text_w = pdf_text_width(part, font_name, float(size))
            draw_x = px + max(0, (width - text_w) / 2)
            c.drawString(draw_x, py - i * leading, part)
        return

    if key in {"owner_full", "case_full"}:
        min_size = 9.2
    elif key in {"owner_id", "property_title"}:
        min_size = 9.2
    else:
        min_size = 9.0

    draw_centered_fit_text(c, line, px, py, width, font_name, float(size), min_size)

def draw_check(c, key: str, page_h: float):
    if key not in CHECK_POS:
        return
    x, y_top, size = CHECK_POS[key]
    px, py = pt_from_top(x, y_top, page_h)
    c.setLineWidth(0.62)
    c.setStrokeColorRGB(0, 0, 0)
    c.line(px - size * 0.42, py - size * 0.02, px - size * 0.10, py - size * 0.32)
    c.line(px - size * 0.10, py - size * 0.32, px + size * 0.44, py + size * 0.38)


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
    deed_owner_info = extract_owner_info_from_case_data(case_data)
    fields: dict[str, str] = {}

    def put(key: str, *values: Any):
        for value in values:
            value = clean(value)
            if value:
                fields[key] = value
                return

    put("owner_name", seller.get("name"), case_data.get("owner_name"), deed_owner_info.get("owner_name"), case_data.get("deed_owner"))
    owner_id_value = normalize_id_text(
        seller.get("id_no")
        or seller.get("identity_no")
        or seller.get("id_number")
        or seller.get("owner_id")
        or seller.get("owner_identity_no")
        or seller.get("身分證字號")
        or seller.get("身份字號")
        or case_data.get("owner_id")
        or case_data.get("owner_identity_no")
        or case_data.get("identity_no")
        or case_data.get("deed_owner_id")
        or case_data.get("身分證字號")
        or case_data.get("身份字號")
        or deed_owner_info.get("owner_id")
        or deed_owner_info.get("owner_identity_no")
        or deed_owner_info.get("identity_no")
    )
    put("owner_id", owner_id_value)
    put("owner_home_phone", seller.get("home_phone"), case_data.get("owner_home_phone"))
    put("owner_company_phone", seller.get("company_phone"), case_data.get("owner_company_phone"))
    put("owner_mobile", seller.get("phone"), seller.get("mobile"), case_data.get("owner_phone"))

    by, bm, bd = parse_owner_birth_parts(
        seller.get("birth_date")
        or seller.get("owner_birth_date")
        or seller.get("出生日期")
        or seller.get("出生年月日")
        or case_data.get("owner_birth_date")
        or case_data.get("birth_date")
        or case_data.get("deed_owner_birth_date")
        or case_data.get("出生日期")
        or case_data.get("出生年月日")
        or deed_owner_info.get("owner_birth_date")
        or deed_owner_info.get("birth_date")
    )
    put("owner_birth_year", case_data.get("owner_birth_year"), deed_owner_info.get("owner_birth_year"), by)
    put("owner_birth_month", case_data.get("owner_birth_month"), deed_owner_info.get("owner_birth_month"), bm)
    put("owner_birth_day", case_data.get("owner_birth_day"), deed_owner_info.get("owner_birth_day"), bd)
    owner_household_address = (
        seller.get("household_address")
        or seller.get("registered_address")
        or seller.get("owner_registered_address")
        or seller.get("owner_household_address")
        or seller.get("hukou_address")
        or seller.get("residence_address")
        or seller.get("戶籍地址")
        or seller.get("戶籍地址_完整")
        or case_data.get("owner_household_address")
        or deed_owner_info.get("owner_household_address")
        or deed_owner_info.get("registered_address")
        or deed_owner_info.get("戶籍地址")
        or case_data.get("registered_address")
        or case_data.get("戶籍地址")
    )
    # 第一個地址欄位是客戶資料的戶籍地址，不再用物件地址代填。
    fill_address_components(fields, "owner", owner_household_address)

    put("property_title", case_data.get("property_title"), case_data.get("ai_sales_title"))
    put("community_name", case_data.get("community_name"))
    fill_address_components(fields, "case", case_data.get("case_address") or seller.get("address"))
    put("floor_total", case_data.get("floor_total"))
    put("basement_total", case_data.get("basement_total"))
    put("floor", case_data.get("floor"))
    put("floor_end", case_data.get("floor_end"))
    layout_counts = parse_layout_counts(case_data.get("layout"))
    put("layout_rooms", case_data.get("rooms"), case_data.get("layout_rooms"), layout_counts.get("rooms"))
    put("layout_halls", case_data.get("halls"), case_data.get("layout_halls"), layout_counts.get("halls"))
    put("layout_baths", case_data.get("baths"), case_data.get("layout_baths"), layout_counts.get("baths"))
    put("layout_balconies", case_data.get("balconies"), case_data.get("layout_balconies"), layout_counts.get("balconies"))
    put("layout_kitchens", case_data.get("kitchens"), case_data.get("layout_kitchens"), layout_counts.get("kitchens"))

    y = clean(case_data.get("completed_minguo_year"))
    m = clean(case_data.get("completed_month"))
    d = clean(case_data.get("completed_day"))
    if not (y and m and d):
        y, m, d = parse_minguo_parts(case_data.get("deed_completed_date") or "")
    put("completed_year", y)
    put("completed_month", m)
    put("completed_day", d)
    put("building_age", case_data.get("building_age"))
    put("facing", case_data.get("building_facing"), case_data.get("facing"), case_data.get("朝向"), case_data.get("座向"))
    put("tower_facing", case_data.get("tower_facing"), case_data.get("大樓朝向"))
    put("balcony_facing", case_data.get("balcony_facing"), case_data.get("陽台朝向"))
    put("sunlight", case_data.get("sunlight"), case_data.get("採光"))

    put("road_width", case_data.get("road_width"), case_data.get("estimated_road_width"), case_data.get("cadastral_road_width"), case_data.get("地籍圖路寬"))
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
    put("case_price", normalize_price_text(case_data.get("case_price") or seller.get("expected_price")))
    put("rent_price", normalize_price_text(case_data.get("rent_price")))
    put("deposit", normalize_price_text(case_data.get("deposit")))
    put("deposit_months", normalize_price_text(case_data.get("deposit_months")))

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
    deed_owner_info = extract_owner_info_from_case_data(case_data)
    checks: dict[str, bool] = {}
    deal = clean(seller.get("deal_type") or case_data.get("deal_type") or "sale").lower()
    checks["deal_rent"] = deal in {"rent", "出租", "租"}
    checks["deal_sale"] = not checks["deal_rent"]

    source = clean(seller.get("source") or case_data.get("source") or case_data.get("customer_source"))
    if "謄本" in source or case_data.get("deed_raw_text") or case_data.get("deed_parsed_json"):
        checks["source_deed"] = True
    elif "親友" in source:
        checks["source_friend"] = True
    elif "來電" in source:
        checks["source_call"] = True
    elif "來店" in source:
        checks["source_store"] = True
    elif "踩線" in source:
        checks["source_line"] = True

    gender = clean(
        seller.get("gender")
        or seller.get("sex")
        or case_data.get("owner_gender")
        or case_data.get("gender")
        or case_data.get("deed_owner_gender")
        or case_data.get("性別")
        or deed_owner_info.get("owner_gender")
        or deed_owner_info.get("gender")
    )
    if not gender:
        owner_id_for_gender = (
            seller.get("id_no")
            or seller.get("identity_no")
            or seller.get("id_number")
            or seller.get("owner_id")
            or seller.get("owner_identity_no")
            or seller.get("身分證字號")
            or seller.get("身份字號")
            or case_data.get("owner_id")
            or case_data.get("owner_identity_no")
            or case_data.get("identity_no")
            or case_data.get("deed_owner_id")
            or case_data.get("身分證字號")
            or case_data.get("身份字號")
            or deed_owner_info.get("owner_id")
        )
        gender = infer_gender_from_id(owner_id_for_gender)
    if gender in {"男", "M", "male", "Male", "MALE", "1"}:
        checks["gender_male"] = True
    elif gender in {"女", "F", "female", "Female", "FEMALE", "2"}:
        checks["gender_female"] = True

    ptype = clean(seller.get("property_type") or case_data.get("property_type"))
    main_use = clean(case_data.get("deed_main_use"))
    material = clean(case_data.get("deed_main_material"))
    floor_total = clean(case_data.get("floor_total"))

    if "土地" in ptype or ("建物" not in ptype and case_data.get("land_ping") and not case_data.get("total_ping")):
        checks["land_building"] = True
        checks["status_land"] = True
    elif "透天" in ptype or (floor_total.isdigit() and int(floor_total) <= 5 and "住宅" in main_use):
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
