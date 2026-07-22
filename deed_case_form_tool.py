# -*- coding: utf-8 -*-
"""
Team M.E｜謄本 PDF → 案件輸入表 PDF 自動填寫工具

功能：
1. 從電子謄本 PDF 抽文字
2. 解析土地 / 建物謄本資料
3. 轉成案件輸入表欄位
4. 用座標定位把資料寫到「案件輸入表-使用中.pdf」
5. 產生填好的案件輸入表 PDF

安裝：
    pip install pypdf reportlab

獨立測試：
    python deed_case_form_tool.py parse H2290000040070a.pdf --json parsed.json
    python deed_case_form_tool.py fill H2290000040070a.pdf 案件輸入表-使用中.pdf filled.pdf

注意：
- 這版針對「電子謄本 PDF」最穩，因為 PDF 裡可直接抽文字。
- 若是掃描圖 PDF，需要先 OCR，再把 OCR 文字交給 parse_deed_text。
- 案件輸入表不是可填式 PDF 表單，所以本工具採「蓋字 + 打勾」方式產生新 PDF。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from io import BytesIO
from pathlib import Path
import argparse
import json
import math
import re
import sys
from typing import Any


# A4 points：案件輸入表是 595 x 842 pt
A4_W = 595.0
A4_H = 842.0


FULLWIDTH_TRANS = str.maketrans({
    "０": "0", "１": "1", "２": "2", "３": "3", "４": "4",
    "５": "5", "６": "6", "７": "7", "８": "8", "９": "9",
    "　": " ",
    "－": "-", "—": "-", "：": ":", "，": ",", "．": ".",
    "／": "/", "（": "(", "）": ")",
})


def clean(value: Any) -> str:
    if value is None:
        return ""
    try:
        if isinstance(value, float) and math.isnan(value):
            return ""
    except Exception:
        pass
    return str(value).strip()


def normalize_text(text: str) -> str:
    text = clean(text).translate(FULLWIDTH_TRANS)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    # 不要把所有換行拿掉，因為謄本是靠行判斷區塊
    text = re.sub(r"[ \t]+", " ", text)
    return text.strip()


def m2_to_ping(m2: float | str) -> float:
    try:
        return round(float(str(m2).replace(",", "")) / 3.305785, 2)
    except Exception:
        return 0.0


def fmt_num(num: float | int | str, digits: int = 2) -> str:
    try:
        value = round(float(str(num).replace(",", "")), digits)
        text = f"{value:.{digits}f}".rstrip("0").rstrip(".")
        return text
    except Exception:
        return clean(num)


def minguo_to_ad_year(year: int) -> int:
    return year + 1911 if year < 1911 else year


def parse_minguo_date(text: str) -> dict[str, str]:
    text = normalize_text(text)
    m = re.search(r"民國\s*(\d{2,4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日", text)
    if not m:
        m = re.search(r"(\d{2,4})\s*[年/-]\s*(\d{1,2})\s*[月/-]\s*(\d{1,2})", text)
    if not m:
        return {"year": "", "month": "", "day": "", "display": ""}
    y = int(m.group(1))
    mm = int(m.group(2))
    dd = int(m.group(3))
    return {
        "year": str(y if y < 1911 else y - 1911),
        "month": str(mm),
        "day": str(dd),
        "display": f"民國{y if y < 1911 else y - 1911}年{mm}月{dd}日",
        "ad_year": str(minguo_to_ad_year(y)),
    }


def calc_age_from_minguo_date(date_text: str, today: datetime | None = None) -> str:
    info = parse_minguo_date(date_text)
    if not info.get("ad_year"):
        return ""
    today = today or datetime.now()
    try:
        age = today.year - int(info["ad_year"])
        # 粗估即可，案件表實務仍可人工修
        if 0 <= age <= 150:
            return str(age)
    except Exception:
        pass
    return ""


def extract_deed_pdf_text_from_bytes(pdf_bytes: bytes) -> str:
    """從電子謄本 PDF bytes 抽文字。"""
    if not pdf_bytes:
        return ""
    try:
        from pypdf import PdfReader
    except Exception as e:
        raise RuntimeError("缺少 pypdf，請先安裝：pip install pypdf") from e

    reader = PdfReader(BytesIO(pdf_bytes))
    pages = []
    for i, page in enumerate(reader.pages, start=1):
        try:
            pages.append(page.extract_text() or "")
        except Exception:
            pages.append("")
    return "\n".join(pages).strip()


def extract_deed_pdf_text(pdf_path: str | Path) -> str:
    return extract_deed_pdf_text_from_bytes(Path(pdf_path).read_bytes())


def split_deed_blocks(text: str) -> list[dict[str, str]]:
    """把整份謄本拆成土地 / 建物區塊。

    電子謄本常見格式：
    - 土地登記第一類謄本...
    - 建物登記第一類謄本...
    每一筆土地或建物可能跨 2-3 頁，所以要用下一個「土地/建物登記第一類謄本」當切點。
    """
    text = normalize_text(text)
    chunks = re.split(r"(?=\n?\s*(?:土地|建物)登記第一類謄本)", "\n" + text)
    blocks = []
    for chunk in chunks:
        chunk = chunk.strip()
        if not chunk:
            continue
        if "土地登記" in chunk[:80]:
            kind = "land"
        elif "建物登記" in chunk[:80]:
            kind = "building"
        else:
            continue
        blocks.append({"kind": kind, "text": chunk})
    return blocks


def first_match(text: str, patterns: list[str], default: str = "", flags=re.S | re.M) -> str:
    for pat in patterns:
        m = re.search(pat, text, flags=flags)
        if m:
            value = clean(m.group(1))
            value = re.split(r"\n\s*\n", value)[0].strip()
            return value
    return default


def parse_right_scope(text: str) -> str:
    value = first_match(text, [
        r"權利範圍:([^\n]+)",
        r"設定權利範圍:([^\n]+)",
    ])
    if not value:
        return ""
    if "全部" in value and "1分之1" in value:
        return "1/1"
    m = re.search(r"(\d+)\s*分之\s*(\d+)", value)
    if m:
        return f"{m.group(2)}/{m.group(1)}"
    return value.replace("*", "").strip()


def parse_mortgages(block_text: str) -> list[dict[str, str]]:
    results = []
    # 用登記次序分段
    parts = re.split(r"(?=\(\d{4}\)登記次序)", block_text)
    for part in parts:
        if "權利種類" not in part and "抵押權" not in part:
            continue
        kind = first_match(part, [r"權利種類:([^\n]+)"])
        creditor = first_match(part, [r"權\s*利\s*人:([^\n]+)"])
        amount = first_match(part, [
            r"擔保債權總金額:新台幣\*+([\d,]+元正)",
            r"擔保債權總金額:([^\n]+)",
        ])
        reg_date = first_match(part, [r"登記日期:([^\n]+?)\s+登記原因"])
        if kind or creditor or amount:
            results.append({
                "kind": kind.replace(" ", ""),
                "creditor": creditor.strip(),
                "amount": amount.strip(),
                "reg_date": reg_date.strip(),
            })
    return results


def parse_land_block(block_text: str) -> dict[str, Any]:
    text = normalize_text(block_text)
    header = first_match(text, [
        r"土地登記第一類謄本[^\n]*\n\s*([^\n]*?段\s+\d{4}-\d{4})地號",
        r"([^\n]*?段\s+\d{4}-\d{4})地號",
    ])
    section = ""
    lot_no = ""
    m = re.search(r"(.+?段)\s+(\d{4}-\d{4})", header)
    if m:
        section = m.group(1).strip()
        lot_no = m.group(2).strip()

    area_m2 = first_match(text, [
        r"面\s*積:\*+\s*([\d.]+)\s*平方公尺",
        r"面\s*積:\s*([\d.]+)\s*平方公尺",
    ])
    use_zone = first_match(text, [r"使用分區:([^\n]+?)\s+使用地類別"])
    use_category = first_match(text, [r"使用地類別:([^\n]+)"])
    current_land_value = first_match(text, [r"公告土地現值:\*+([\d,]+元/平方公尺|[\d,]+元／平方公尺)"])
    right_scope = parse_right_scope(text)
    deed_no = first_match(text, [r"權狀字號:([^\n]+)"])
    restrictions = []
    if "禁止處分" in text:
        line = first_match(text, [r"其他登記事項:（限制登記事項）(.+?)(?=\n\s*\*|\n\s*土地他項權利部|\Z)"])
        restrictions.append("禁止處分登記" + (f"：{line}" if line else ""))

    mortgages = parse_mortgages(text)

    return {
        "section": section,
        "lot_no": lot_no,
        "full_lot_no": f"{section} {lot_no}".strip(),
        "area_m2": float(area_m2) if area_m2 else 0.0,
        "area_ping": m2_to_ping(area_m2) if area_m2 else 0.0,
        "use_zone": use_zone.replace("（空白）", "").strip(),
        "use_category": use_category.replace("（空白）", "").strip(),
        "current_land_value": current_land_value,
        "right_scope": right_scope,
        "deed_no": deed_no,
        "restrictions": restrictions,
        "mortgages": mortgages,
    }


def parse_building_block(block_text: str) -> dict[str, Any]:
    text = normalize_text(block_text)
    header = first_match(text, [
        r"建物登記第一類謄本[^\n]*\n\s*([^\n]*?段\s+\d{5}-\d{3})建號",
        r"([^\n]*?段\s+\d{5}-\d{3})建號",
    ])
    section = ""
    building_no = ""
    m = re.search(r"(.+?段)\s+(\d{5}-\d{3})", header)
    if m:
        section = m.group(1).strip()
        building_no = m.group(2).strip()

    address = first_match(text, [r"建物門牌:([^\n]+)"])
    land_lots = first_match(text, [r"建物坐落地號:([^\n]+)"])
    main_use = first_match(text, [r"主要用途:([^\n]+)"])
    material = first_match(text, [r"主要建材:([^\n]+)"])

    floor_total = ""
    main_total_m2 = 0.0
    m = re.search(r"層\s*數:\s*0*(\d+)層\s+總面積:\*+\s*([\d.]+)\s*平方公尺", text)
    if m:
        floor_total = m.group(1)
        main_total_m2 = float(m.group(2))

    floor_details = []
    # 一層 層次面積：40.21、二層 38.67...
    for fm in re.finditer(r"([一二三四五六七八九十\d]+層)\s*(?:層次面積:)?\*+\s*([\d.]+)\s*平方公尺", text):
        floor_details.append({
            "floor": fm.group(1),
            "area_m2": float(fm.group(2)),
            "area_ping": m2_to_ping(fm.group(2)),
        })

    completed = first_match(text, [r"(建築完成日期:民國\s*\d+\s*年\s*\d+\s*月\s*\d+\s*日)"])
    if completed.startswith("建築完成日期:"):
        completed = completed.replace("建築完成日期:", "").strip()
    completed_info = parse_minguo_date(completed)

    # 附屬建物用途：陽台 面積：11.23平方公尺
    attached_records = []
    attached_total_m2 = 0.0
    attached_part = ""
    if "附屬建物用途" in text:
        attached_part = re.split(r"附屬建物用途:", text, maxsplit=1)[1]
        attached_part = re.split(r"其他登記事項|建物所有權部", attached_part, maxsplit=1)[0]
        for am in re.finditer(r"([^\n]*?)\*+\s*([\d.]+)\s*平方公尺", attached_part):
            use = am.group(1).replace("面積:", "").strip()
            area = float(am.group(2))
            attached_records.append({
                "use": use,
                "area_m2": area,
                "area_ping": m2_to_ping(area),
            })
            attached_total_m2 += area

    use_license = first_match(text, [r"使用執照字號:([^\n]+)"])
    right_scope = parse_right_scope(text)
    deed_no = first_match(text, [r"權狀字號:([^\n]+)"])

    restrictions = []
    if "禁止處分" in text:
        line = first_match(text, [r"其他登記事項:（限制登記事項）(.+?)(?=\n\s*\*|\n\s*建物他項權利部|\Z)"])
        restrictions.append("禁止處分登記" + (f"：{line}" if line else ""))

    mortgages = parse_mortgages(text)

    main_ping = m2_to_ping(main_total_m2) if main_total_m2 else 0.0
    attached_ping = m2_to_ping(attached_total_m2) if attached_total_m2 else 0.0
    total_registered_m2 = main_total_m2 + attached_total_m2
    total_registered_ping = m2_to_ping(total_registered_m2) if total_registered_m2 else 0.0

    return {
        "section": section,
        "building_no": building_no,
        "full_building_no": f"{section} {building_no}".strip(),
        "address": address,
        "land_lots": land_lots,
        "main_use": main_use,
        "material": material,
        "floor_total": floor_total,
        "main_total_m2": main_total_m2,
        "main_ping": main_ping,
        "attached_total_m2": round(attached_total_m2, 2),
        "attached_ping": attached_ping,
        "total_registered_m2": round(total_registered_m2, 2),
        "total_registered_ping": total_registered_ping,
        "floor_details": floor_details,
        "attached_records": attached_records,
        "completed_date": completed_info.get("display") or completed,
        "completed_minguo_year": completed_info.get("year", ""),
        "completed_month": completed_info.get("month", ""),
        "completed_day": completed_info.get("day", ""),
        "building_age": calc_age_from_minguo_date(completed),
        "use_license": use_license,
        "right_scope": right_scope,
        "deed_no": deed_no,
        "restrictions": restrictions,
        "mortgages": mortgages,
    }



def parse_owner_personal_info(text: str) -> dict[str, str]:
    """從謄本全文抓所有權人個資。

    支援你貼的格式：
    所有權人：陳龍騰
    統一編號：C120801873
    出生日期：民國066年01月04日
    住址：嘉義市東區圳頭里23鄰五福街185巷26號之1

    「住址」會視為客戶資料區的戶籍地址 / 登記住址。
    """
    text = normalize_text(text)
    info = {
        "owner_name": "",
        "owner_id": "",
        "owner_identity_no": "",
        "owner_gender": "",
        "owner_birth_date": "",
        "owner_birth_year": "",
        "owner_birth_month": "",
        "owner_birth_day": "",
        "owner_household_address": "",
        "registered_address": "",
        "household_address": "",
    }

    # 優先從所有權部抓；抓不到就全篇抓，因為有些 PDF 斷行會讓區塊標題遺失。
    owner_block = first_match(text, [
        r"(?:土地所有權部|建物所有權部)(.*?)(?:土地他項權利部|建物他項權利部|共同擔保|標示部|本謄本僅係|$)",
    ]) or text

    info["owner_name"] = first_match(owner_block, [
        r"(?:所有權人|權利人|登記名義人|納稅義務人)\s*[:：]?\s*([\u4e00-\u9fff]{2,8})(?=\s|\n|,|，|\*)",
        r"(?:所有權人|權利人|登記名義人|納稅義務人)\s*[:：]?\s*([^\n\s，,：:]{2,12})",
    ])

    raw_id = first_match(owner_block, [
        r"(?:國民身分證統一編號|身分證統一編號|身分證明文件字號|身分證字號|身份字號|統一編號)\s*[:：]?\s*([A-Z][12][0-9]{8})",
        r"\b([A-Z][12][0-9]{8})\b",
    ])
    raw_id = raw_id.upper().replace(" ", "")
    info["owner_id"] = raw_id
    info["owner_identity_no"] = raw_id

    gender = first_match(owner_block, [r"性\s*別\s*[:：]?\s*([男女])"])
    if gender not in {"男", "女"} and len(raw_id) >= 2:
        if raw_id[1] == "1":
            gender = "男"
        elif raw_id[1] == "2":
            gender = "女"
    info["owner_gender"] = gender if gender in {"男", "女"} else ""

    birth = first_match(owner_block, [
        r"(?:出生日期|出生年月日|出生)\s*[:：]?\s*(民國\s*\d{2,4}\s*年\s*\d{1,2}\s*月\s*\d{1,2}\s*日)",
        r"(?:出生日期|出生年月日|出生)\s*[:：]?\s*(\d{2,4}\s*[年/-]\s*\d{1,2}\s*[月/-]\s*\d{1,2}\s*日?)",
    ])
    info["owner_birth_date"] = birth
    if birth:
        b = birth.translate(str.maketrans({
            "０":"0","１":"1","２":"2","３":"3","４":"4",
            "５":"5","６":"6","７":"7","８":"8","９":"9",
        }))
        m = re.search(r"民國\s*(\d{2,4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})", b) or re.search(r"(\d{2,4})\s*[年/-]\s*(\d{1,2})\s*[月/-]\s*(\d{1,2})", b)
        if m:
            y = int(m.group(1))
            if y > 1911:
                y -= 1911
            info["owner_birth_year"] = str(y)
            info["owner_birth_month"] = str(int(m.group(2)))
            info["owner_birth_day"] = str(int(m.group(3)))

    address = first_match(owner_block, [
        r"(?:戶籍地址|戶籍住址|登記住址|住\s*址|住\s*所|通訊地址)\s*[:：]?\s*([^\n]+)",
    ])
    if address:
        address = re.split(
            r"\s+(?:權利範圍|權狀字號|登記日期|登記原因|其他登記事項|前次移轉|管理者)\s*[:：]?",
            address,
        )[0].strip()
        address = re.sub(r"\s+", "", address)
    if address and not any(k in address for k in ["銀行", "股份有限公司", "有限公司", "抵押權"]):
        info["owner_household_address"] = address
        info["registered_address"] = address
        info["household_address"] = address

    return {k: v for k, v in info.items() if clean(v)}


def parse_cadastral_map_text(text: str) -> dict[str, str]:
    """解析地籍圖謄本。

    地籍圖可讀：
    - 土地坐落
    - 比例尺
    - 周邊地號

    但路寬不自動亂填；除非你另外手動輸入 road_width。
    因為地籍圖也會註明「實地界址以複丈鑑界結果為準」。
    """
    text = normalize_text(text)
    if "地籍圖謄本" not in text and "比例尺" not in text:
        return {}

    out = {}
    lot = first_match(text, [r"土地坐落[:：]\s*([^\n]+)"])
    scale = first_match(text, [r"比例尺[:：]\s*1\s*/\s*(\d+)"])
    if lot:
        out["cadastral_lot"] = lot
        out["land_lot_no"] = lot
    if scale:
        out["cadastral_scale"] = f"1/{scale}"

    # 只做輔助判斷，不自動填面臨路寬。
    out["cadastral_road_access"] = "需人工確認"
    out["road_width_source"] = "地籍圖輔助判斷"
    out["cadastral_note"] = (
        "已上傳地籍圖。地籍圖可輔助判斷是否臨路與估算路寬；"
        "但實際界址與可通行寬度仍需現場確認/複丈鑑界，因此面臨路寬先不自動填。"
    )
    return out


def parse_deed_text(text: str) -> dict[str, Any]:
    text = normalize_text(text)
    owner_info = parse_owner_personal_info(text)
    cadastral_info = parse_cadastral_map_text(text)
    blocks = split_deed_blocks(text)

    land_records = []
    building_records = []

    for block in blocks:
        if block["kind"] == "land":
            rec = parse_land_block(block["text"])
            if rec.get("lot_no") or rec.get("area_m2"):
                land_records.append(rec)
        elif block["kind"] == "building":
            rec = parse_building_block(block["text"])
            if rec.get("building_no") or rec.get("address"):
                building_records.append(rec)

    total_land_m2 = round(sum(float(r.get("area_m2") or 0) for r in land_records), 2)
    total_land_ping = m2_to_ping(total_land_m2) if total_land_m2 else 0.0

    building = building_records[0] if building_records else {}

    all_mortgages = []
    all_restrictions = []
    for rec in land_records + building_records:
        all_mortgages.extend(rec.get("mortgages") or [])
        all_restrictions.extend(rec.get("restrictions") or [])

    mortgage_summary_parts = []
    seen_mort = set()
    for m in all_mortgages:
        key = (m.get("kind", ""), m.get("creditor", ""), m.get("amount", ""))
        if key in seen_mort:
            continue
        seen_mort.add(key)
        label = " / ".join([x for x in [m.get("kind"), m.get("creditor"), m.get("amount")] if x])
        if label:
            mortgage_summary_parts.append(label)

    warning_parts = []
    if all_restrictions:
        warning_parts.append("有禁止處分登記，需確認是否可移轉及塗銷條件。")
    if mortgage_summary_parts:
        warning_parts.append("有他項權利/最高限額抵押權：" + "；".join(mortgage_summary_parts[:5]))

    parsed = {
        "raw_text": text,
        "land_records": land_records,
        "building_records": building_records,
        "total_land_m2": total_land_m2,
        "total_land_ping": total_land_ping,
        "building_record": building,
        "warnings": warning_parts,
        "owner_info": owner_info,
        "cadastral_info": cadastral_info,
    }

    case_data = to_case_data(parsed)
    parsed["case_data"] = case_data
    return parsed


def to_case_data(parsed: dict[str, Any]) -> dict[str, Any]:
    lands = parsed.get("land_records") or []
    building = parsed.get("building_record") or {}
    warnings = parsed.get("warnings") or []
    owner_info = parsed.get("owner_info") or {}
    cadastral_info = parsed.get("cadastral_info") or {}

    land_lot_no = "、".join([r.get("full_lot_no") or r.get("lot_no") for r in lands if r.get("full_lot_no") or r.get("lot_no")])
    building_no = building.get("full_building_no") or building.get("building_no") or ""

    land_right_scope = ""
    if lands:
        scopes = [clean(r.get("right_scope")) for r in lands if clean(r.get("right_scope"))]
        land_right_scope = "、".join(dict.fromkeys(scopes))

    deed_parsed_lines = []
    if owner_info.get("owner_name"):
        deed_parsed_lines.append(f"所有權人：{owner_info.get('owner_name')}")
    if owner_info.get("owner_id"):
        deed_parsed_lines.append("身分證字號：已解析")
    if owner_info.get("owner_gender"):
        deed_parsed_lines.append(f"性別：{owner_info.get('owner_gender')}")
    if owner_info.get("owner_birth_date"):
        deed_parsed_lines.append(f"出生日期：{owner_info.get('owner_birth_date')}")
    if land_lot_no:
        deed_parsed_lines.append(f"地號：{land_lot_no}")
    if building_no:
        deed_parsed_lines.append(f"建號：{building_no}")
    if building.get("address"):
        deed_parsed_lines.append(f"建物門牌：{building.get('address')}")
    if building.get("main_use"):
        deed_parsed_lines.append(f"主要用途：{building.get('main_use')}")
    if building.get("material"):
        deed_parsed_lines.append(f"主要建材：{building.get('material')}")
    if building.get("completed_date"):
        deed_parsed_lines.append(f"建築完成日期：{building.get('completed_date')}")
    if parsed.get("total_land_ping"):
        deed_parsed_lines.append(f"土地總面積：約 {fmt_num(parsed.get('total_land_ping'))} 坪（{fmt_num(parsed.get('total_land_m2'))} 平方公尺）")
    if building.get("total_registered_ping"):
        deed_parsed_lines.append(f"建物登記總面積：約 {fmt_num(building.get('total_registered_ping'))} 坪")
    if cadastral_info.get("cadastral_lot"):
        deed_parsed_lines.append(f"地籍圖：{cadastral_info.get('cadastral_lot')}")
    if cadastral_info.get("cadastral_scale"):
        deed_parsed_lines.append(f"地籍圖比例尺：{cadastral_info.get('cadastral_scale')}")
    if warnings:
        deed_parsed_lines.append("注意事項：" + "；".join(warnings))

    case_note = "；".join(warnings)
    if building.get("use_license"):
        case_note = (case_note + "\n" if case_note else "") + f"使用執照字號：{building.get('use_license')}"

    return {
        "owner_name": owner_info.get("owner_name", ""),
        "owner_id": owner_info.get("owner_id", ""),
        "owner_identity_no": owner_info.get("owner_identity_no") or owner_info.get("owner_id", ""),
        "owner_gender": owner_info.get("owner_gender", ""),
        "owner_birth_date": owner_info.get("owner_birth_date", ""),
        "owner_birth_year": owner_info.get("owner_birth_year", ""),
        "owner_birth_month": owner_info.get("owner_birth_month", ""),
        "owner_birth_day": owner_info.get("owner_birth_day", ""),
        "owner_household_address": owner_info.get("owner_household_address", ""),
        "cadastral_lot": cadastral_info.get("cadastral_lot", ""),
        "cadastral_scale": cadastral_info.get("cadastral_scale", ""),
        "cadastral_note": cadastral_info.get("cadastral_note", ""),
        "case_address": building.get("address", ""),
        "land_lot_no": land_lot_no,
        "building_no": building_no,
        "deed_main_use": building.get("main_use", ""),
        "deed_main_material": building.get("material", ""),
        "floor_total": building.get("floor_total", ""),
        "main_ping": fmt_num(building.get("main_ping")) if building.get("main_ping") else "",
        "attached_ping": fmt_num(building.get("attached_ping")) if building.get("attached_ping") else "",
        "public_ping": "",
        "parking_ping": "",
        "total_ping": fmt_num(building.get("total_registered_ping")) if building.get("total_registered_ping") else "",
        "land_ping": fmt_num(parsed.get("total_land_ping")) if parsed.get("total_land_ping") else "",
        "deed_right_scope": land_right_scope or building.get("right_scope", ""),
        "deed_completed_date": building.get("completed_date", ""),
        "completed_minguo_year": building.get("completed_minguo_year", ""),
        "completed_month": building.get("completed_month", ""),
        "completed_day": building.get("completed_day", ""),
        "building_age": building.get("building_age", ""),
        "deed_mortgage_note": "；".join(warnings),
        "deed_parsed_note": "\n".join(deed_parsed_lines),
        "case_note": case_note,
    }


# ===== 案件輸入表座標 =====
# 以「左下角為原點」的 PDF points 座標系統。
# 為方便調整，這裡用 top-left 直覺輸入，再轉成 PDF y。
def pt_from_top(x: float, y_top: float) -> tuple[float, float]:
    return x, A4_H - y_top


# text: x/y_top/font/box width
TEXT_POS = {
    # 客戶資料
    "owner_name": (46, 123, 9, 92),
    "owner_mobile": (418, 151, 9, 120),
    "owner_address": (63, 176, 9, 455),

    # 基本資料
    "property_title": (84, 215, 9, 135),
    "community_name": (500, 215, 9, 72),
    "case_address": (83, 333, 9, 465),
    "floor_total": (94, 350, 9, 34),
    "floor": (95, 365, 9, 36),
    "layout": (238, 365, 9, 185),
    "completed_year": (130, 382, 9, 24),
    "completed_month": (174, 382, 9, 18),
    "completed_day": (211, 382, 9, 18),
    "building_age": (303, 382, 9, 25),
    "facing": (511, 290, 9, 28),

    # 結構 / 車位
    "road_width": (96, 459, 9, 30),
    "management_fee": (95, 502, 9, 50),
    "elevator_count": (393, 502, 9, 26),
    "households_per_floor": (516, 502, 9, 28),

    # 面積 / 金額
    "total_ping": (100, 579, 9, 45),
    "main_ping": (203, 579, 9, 45),
    "attached_ping": (294, 579, 9, 45),
    "public_ping": (384, 579, 9, 45),
    "parking_ping": (486, 579, 9, 45),
    "land_ping": (100, 604, 9, 45),
    "base_land_ping": (209, 604, 9, 45),
    "land_share_ping": (362, 604, 9, 45),
    "case_price": (68, 630, 9, 60),
    "rent_price": (208, 630, 9, 60),
    "deposit": (330, 630, 9, 55),
    "deposit_months": (501, 630, 9, 45),

    # 學區環境
    "elementary_school": (95, 666, 9, 78),
    "junior_high_school": (260, 666, 9, 80),
    "market": (459, 666, 9, 80),
    "park": (95, 684, 9, 78),
    "medical": (260, 684, 9, 80),
    "station": (459, 684, 9, 80),
    "builder": (95, 705, 9, 78),
    "business_area": (260, 705, 9, 80),

    # 備註
    "feature_note": (134, 750, 8, 430),
    "special_note": (134, 805, 8, 430),
}


# checkbox: center x/y_top/size
CHECK_POS = {
    # header
    "deal_sale": (84, 51, 7),
    "deal_rent": (124, 51, 7),
    "mandate_exclusive": (264, 51, 7),
    "mandate_general": (314, 51, 7),
    "source_deed": (301, 98, 7),

    # 案件型態
    "type_apartment": (84, 232, 7),
    "type_huaxia": (143, 232, 7),
    "type_toutian": (191, 232, 7),
    "type_villa": (238, 232, 7),
    "type_farmhouse": (285, 232, 7),
    "type_store": (330, 232, 7),
    "type_suite": (374, 232, 7),
    "type_factory": (421, 232, 7),

    # 土地型態
    "land_building": (84, 249, 7),
    "land_commercial": (145, 249, 7),
    "land_industrial": (212, 249, 7),
    "land_agricultural": (279, 249, 7),
    "land_protected": (348, 249, 7),
    "land_other": (417, 249, 7),

    # 現況
    "status_empty": (84, 267, 7),
    "status_self_use": (134, 267, 7),
    "status_rented": (184, 267, 7),
    "status_structure": (234, 267, 7),
    "status_land": (290, 267, 7),
    "status_other": (338, 267, 7),

    # 售屋動機
    "reason_change_house": (84, 285, 7),
    "reason_work": (134, 285, 7),
    "reason_school": (184, 285, 7),
    "reason_immigrate": (234, 285, 7),
    "reason_cash": (286, 285, 7),
    "reason_other": (362, 285, 7),

    # 建物結構
    "structure_brick": (82, 442, 7),
    "structure_reinforced_brick": (139, 442, 7),
    "structure_rc": (246, 442, 7),
    "structure_src": (359, 442, 7),
    "structure_stone": (454, 442, 7),
    "structure_other": (500, 442, 7),

    # 用途
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


def infer_checks(case_data: dict[str, Any], seller: dict[str, Any] | None = None) -> dict[str, bool]:
    seller = seller or {}
    checks = {}
    deal = clean(seller.get("deal_type") or case_data.get("deal_type") or "sale").lower()
    checks["deal_rent"] = deal in {"rent", "出租", "租"}
    checks["deal_sale"] = not checks["deal_rent"]
    checks["source_deed"] = True

    ptype = clean(seller.get("property_type") or case_data.get("property_type"))
    floor_total = clean(case_data.get("floor_total"))
    main_use = clean(case_data.get("deed_main_use"))
    material = clean(case_data.get("deed_main_material"))

    if "透天" in ptype or (floor_total.isdigit() and int(floor_total) <= 5 and ("住宅" in main_use)):
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
    if "辦公" in main_use or "事務所" in main_use:
        checks["use_office"] = True

    return checks


def build_fill_fields(case_data: dict[str, Any], seller: dict[str, Any] | None = None) -> dict[str, str]:
    seller = seller or {}
    fields = {}

    def put(k, *vals):
        for v in vals:
            v = clean(v)
            if v:
                fields[k] = v
                return

    put("owner_name", seller.get("name"))
    put("owner_mobile", seller.get("phone"))
    put("owner_address", seller.get("contact_address"), seller.get("address"))

    put("property_title", case_data.get("property_title"), case_data.get("ai_sales_title"))
    put("community_name", case_data.get("community_name"))
    put("case_address", case_data.get("case_address"), seller.get("address"))
    put("floor_total", case_data.get("floor_total"))
    put("floor", case_data.get("floor"))
    put("layout", case_data.get("layout"))

    put("completed_year", case_data.get("completed_minguo_year"))
    put("completed_month", case_data.get("completed_month"))
    put("completed_day", case_data.get("completed_day"))
    if not fields.get("completed_year") and case_data.get("deed_completed_date"):
        info = parse_minguo_date(case_data.get("deed_completed_date"))
        fields["completed_year"] = info.get("year", "")
        fields["completed_month"] = info.get("month", "")
        fields["completed_day"] = info.get("day", "")
    put("building_age", case_data.get("building_age"))
    put("facing", case_data.get("facing"))

    put("total_ping", case_data.get("total_ping"))
    put("main_ping", case_data.get("main_ping"))
    put("attached_ping", case_data.get("attached_ping"))
    put("public_ping", case_data.get("public_ping"))
    put("parking_ping", case_data.get("parking_ping"))
    put("land_ping", case_data.get("land_ping"))
    put("base_land_ping", case_data.get("land_ping"))
    put("land_share_ping", case_data.get("land_ping"))
    put("case_price", case_data.get("case_price"), seller.get("expected_price"))
    put("rent_price", case_data.get("rent_price"))
    put("deposit", case_data.get("deposit"))
    put("deposit_months", case_data.get("deposit_months"))

    feature_lines = []
    for k in ("ai_feature_note", "property_highlight_note", "life_note", "target_customer_note"):
        if case_data.get(k):
            feature_lines.append(clean(case_data.get(k)))
    if not feature_lines and case_data.get("deed_parsed_note"):
        feature_lines.append(clean(case_data.get("deed_parsed_note"))[:180])
    fields["feature_note"] = "\n".join(feature_lines)[:260]

    special_lines = []
    if case_data.get("deed_mortgage_note"):
        special_lines.append(clean(case_data.get("deed_mortgage_note")))
    if case_data.get("case_note"):
        special_lines.append(clean(case_data.get("case_note")))
    fields["special_note"] = "\n".join(special_lines)[:260]

    return fields


def draw_wrapped_text(c, text: str, x: float, y: float, width: float, font: str, size: float, leading: float | None = None, max_lines: int = 3):
    text = clean(text)
    if not text:
        return
    leading = leading or size + 2
    # 粗略用中文字寬 1em、英數 0.55em 估算換行，實務表格填寫夠用。
    def unit_len(s):
        return sum(1.0 if ord(ch) > 127 else 0.55 for ch in s)

    max_units = max(1, int(width / max(size * 0.9, 1)))
    lines = []
    for raw_line in text.splitlines():
        current = ""
        for ch in raw_line:
            if unit_len(current + ch) > max_units:
                lines.append(current)
                current = ch
            else:
                current += ch
        if current:
            lines.append(current)
    lines = lines[:max_lines]
    c.setFont(font, size)
    for i, line in enumerate(lines):
        c.drawString(x, y - i * leading, line)


def draw_check(c, x: float, y: float, size: float = 7.0):
    # 用線段畫勾，避免中文字型不支援打勾符號。
    c.setLineWidth(1.2)
    c.line(x - size * 0.45, y, x - size * 0.12, y - size * 0.35)
    c.line(x - size * 0.12, y - size * 0.35, x + size * 0.48, y + size * 0.45)


def fill_case_form_pdf_bytes(
    template_pdf: str | Path,
    case_data: dict[str, Any],
    seller: dict[str, Any] | None = None,
    extra_fields: dict[str, str] | None = None,
    extra_checks: dict[str, bool] | None = None,
) -> bytes:
    try:
        from pypdf import PdfReader, PdfWriter
        from reportlab.pdfgen import canvas
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.cidfonts import UnicodeCIDFont
    except Exception as e:
        raise RuntimeError("缺少套件，請先安裝：pip install pypdf reportlab") from e

    template_pdf = Path(template_pdf)
    if not template_pdf.exists():
        raise FileNotFoundError(f"找不到案件輸入表 PDF 範本：{template_pdf}")

    seller = seller or {}
    fields = build_fill_fields(case_data, seller)
    if extra_fields:
        fields.update({k: clean(v) for k, v in extra_fields.items() if clean(v)})

    checks = infer_checks(case_data, seller)
    if extra_checks:
        checks.update(extra_checks)

    reader = PdfReader(str(template_pdf))
    page = reader.pages[0]
    width = float(page.mediabox.width)
    height = float(page.mediabox.height)

    packet = BytesIO()
    c = canvas.Canvas(packet, pagesize=(width, height))
    pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))
    font_name = "STSong-Light"

    # 文字
    for key, value in fields.items():
        if not value or key not in TEXT_POS:
            continue
        x, y_top, size, box_w = TEXT_POS[key]
        px, py = pt_from_top(x, y_top)
        max_lines = 1
        if key in {"feature_note", "special_note"}:
            max_lines = 3
        draw_wrapped_text(c, str(value), px, py, box_w, font_name, size, max_lines=max_lines)

    # 勾選框
    for key, enabled in checks.items():
        if not enabled or key not in CHECK_POS:
            continue
        x, y_top, size = CHECK_POS[key]
        px, py = pt_from_top(x, y_top)
        draw_check(c, px, py, size)

    c.save()
    packet.seek(0)
    overlay_reader = PdfReader(packet)

    page.merge_page(overlay_reader.pages[0])
    writer = PdfWriter()
    writer.add_page(page)

    out = BytesIO()
    writer.write(out)
    return out.getvalue()


def fill_case_form_pdf_file(template_pdf: str | Path, output_pdf: str | Path, case_data: dict[str, Any], seller: dict[str, Any] | None = None):
    data = fill_case_form_pdf_bytes(template_pdf, case_data, seller=seller)
    output_pdf = Path(output_pdf)
    output_pdf.parent.mkdir(parents=True, exist_ok=True)
    output_pdf.write_bytes(data)
    return output_pdf


def make_debug_grid_pdf(template_pdf: str | Path, output_pdf: str | Path, step: int = 25):
    """產生座標格線，之後要微調欄位位置會很好用。"""
    try:
        from pypdf import PdfReader, PdfWriter
        from reportlab.pdfgen import canvas
    except Exception as e:
        raise RuntimeError("缺少套件，請先安裝：pip install pypdf reportlab") from e

    reader = PdfReader(str(template_pdf))
    page = reader.pages[0]
    width = float(page.mediabox.width)
    height = float(page.mediabox.height)

    packet = BytesIO()
    c = canvas.Canvas(packet, pagesize=(width, height))
    c.setStrokeColorRGB(1, 0, 0)
    c.setFillColorRGB(1, 0, 0)
    c.setFont("Helvetica", 5)
    c.setLineWidth(0.2)

    x = 0
    while x <= width:
        c.line(x, 0, x, height)
        c.drawString(x + 1, height - 8, str(int(x)))
        x += step

    y = 0
    while y <= height:
        c.line(0, height - y, width, height - y)
        c.drawString(2, height - y + 1, f"T{int(y)}")
        y += step

    c.save()
    packet.seek(0)
    overlay_reader = PdfReader(packet)
    page.merge_page(overlay_reader.pages[0])
    writer = PdfWriter()
    writer.add_page(page)
    out = Path(output_pdf)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("wb") as f:
        writer.write(f)
    return out


def parse_pdf_to_case_data(pdf_path: str | Path) -> dict[str, Any]:
    text = extract_deed_pdf_text(pdf_path)
    return parse_deed_text(text)


def main(argv=None):
    parser = argparse.ArgumentParser(description="謄本 PDF 解析與案件輸入表填寫工具")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_parse = sub.add_parser("parse", help="解析謄本 PDF 為 JSON")
    p_parse.add_argument("deed_pdf")
    p_parse.add_argument("--json", dest="json_out", default="")

    p_fill = sub.add_parser("fill", help="解析謄本 PDF 並填入案件輸入表 PDF")
    p_fill.add_argument("deed_pdf")
    p_fill.add_argument("template_pdf")
    p_fill.add_argument("output_pdf")
    p_fill.add_argument("--seller-name", default="")
    p_fill.add_argument("--seller-phone", default="")
    p_fill.add_argument("--price", default="")
    p_fill.add_argument("--property-type", default="")

    p_grid = sub.add_parser("grid", help="產生座標格線 PDF，方便調整欄位位置")
    p_grid.add_argument("template_pdf")
    p_grid.add_argument("output_pdf")
    p_grid.add_argument("--step", type=int, default=25)

    args = parser.parse_args(argv)

    if args.cmd == "parse":
        parsed = parse_pdf_to_case_data(args.deed_pdf)
        payload = json.dumps(parsed, ensure_ascii=False, indent=2)
        if args.json_out:
            Path(args.json_out).write_text(payload, encoding="utf-8")
        print(payload)

    elif args.cmd == "fill":
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
        fill_case_form_pdf_file(args.template_pdf, args.output_pdf, case_data, seller=seller)
        print(f"已產生：{args.output_pdf}")

    elif args.cmd == "grid":
        make_debug_grid_pdf(args.template_pdf, args.output_pdf, step=args.step)
        print(f"已產生座標格線：{args.output_pdf}")


if __name__ == "__main__":
    main()
