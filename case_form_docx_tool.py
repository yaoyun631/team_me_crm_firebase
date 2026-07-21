# -*- coding: utf-8 -*-
"""
Team M.E｜謄本 PDF → Word 案件輸入表（直接 key 進 Word 版）

這版改用「案件輸入表-使用中.docx」作為 Word 範本：
- 不使用 PDF 座標蓋字
- 不使用圖片背景版型
- 直接把資料寫進 Word 文字內容裡
- 全文件字體改為標楷體 / DFKai-SB
"""

from __future__ import annotations

from io import BytesIO
from pathlib import Path
import math
import re
import zipfile
from typing import Any

from lxml import etree

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
XML_NS = "http://www.w3.org/XML/1998/namespace"
NS = {"w": W_NS}

DFKAI_ASCII = "DFKai-SB"
DFKAI_EAST_ASIA = "標楷體"


def clean(value: Any) -> str:
    if value is None:
        return ""
    try:
        if isinstance(value, float) and math.isnan(value):
            return ""
    except Exception:
        pass
    return str(value).strip()


def _fmt(value: Any, digits: int = 2) -> str:
    value = clean(value)
    if not value:
        return ""
    try:
        f = round(float(value.replace(",", "")), digits)
        return f"{f:.{digits}f}".rstrip("0").rstrip(".")
    except Exception:
        return value


def _w(tag: str) -> str:
    return f"{{{W_NS}}}{tag}"


def _set_rpr_font(rpr, size_half_points: int | None = None, bold: bool | None = None):
    if rpr is None:
        return
    rfonts = rpr.find(_w("rFonts"))
    if rfonts is None:
        rfonts = etree.SubElement(rpr, _w("rFonts"))
    rfonts.set(_w("ascii"), DFKAI_ASCII)
    rfonts.set(_w("hAnsi"), DFKAI_ASCII)
    rfonts.set(_w("eastAsia"), DFKAI_EAST_ASIA)
    rfonts.set(_w("cs"), DFKAI_ASCII)

    if size_half_points:
        sz = rpr.find(_w("sz"))
        if sz is None:
            sz = etree.SubElement(rpr, _w("sz"))
        sz.set(_w("val"), str(size_half_points))
        szcs = rpr.find(_w("szCs"))
        if szcs is None:
            szcs = etree.SubElement(rpr, _w("szCs"))
        szcs.set(_w("val"), str(size_half_points))

    if bold is not None:
        b = rpr.find(_w("b"))
        if bold:
            if b is None:
                etree.SubElement(rpr, _w("b"))
        elif b is not None:
            rpr.remove(b)


def set_all_fonts_to_dfkai(root):
    for rpr in root.xpath(".//w:rPr", namespaces=NS):
        _set_rpr_font(rpr)
    for ppr in root.xpath(".//w:pPr", namespaces=NS):
        rpr = ppr.find(_w("rPr"))
        if rpr is not None:
            _set_rpr_font(rpr)


def paragraph_text(p) -> str:
    return "".join(p.xpath(".//w:t/text()", namespaces=NS))


def replace_paragraph_text(p, text: str, size_half_points: int = 18, bold: bool | None = None):
    # 保留段落格式，只替換文字內容，文字就是 Word 裡可編輯的 run。
    for child in list(p):
        if child.tag != _w("pPr"):
            p.remove(child)

    r = etree.SubElement(p, _w("r"))
    rpr = etree.SubElement(r, _w("rPr"))
    _set_rpr_font(rpr, size_half_points=size_half_points, bold=bold)

    parts = str(text or "").split("\n")
    for i, part in enumerate(parts):
        if i:
            etree.SubElement(r, _w("br"))
        t = etree.SubElement(r, _w("t"))
        t.set(f"{{{XML_NS}}}space", "preserve")
        t.text = part


def checkbox(enabled: bool) -> str:
    # 使用黑方塊當勾選，避免某些電腦缺少 ☑ 字形。
    return "■" if enabled else "□"


def infer_property_type(case_data: dict, seller: dict | None = None) -> str:
    seller = seller or {}
    ptype = clean(seller.get("property_type") or case_data.get("property_type"))
    main_use = clean(case_data.get("deed_main_use"))
    floor_total = clean(case_data.get("floor_total"))
    if ptype:
        return ptype
    if floor_total.isdigit() and int(floor_total) <= 5 and ("住宅" in main_use):
        return "透天"
    if "店" in main_use:
        return "店面"
    if "工業" in main_use or "廠" in main_use:
        return "廠房"
    return ""


def build_case_form_lines(case_data: dict[str, Any], seller: dict[str, Any] | None = None) -> dict[str, tuple[str, int, bool | None]]:
    seller = seller or {}

    owner_name = clean(seller.get("name") or case_data.get("owner_name") or case_data.get("deed_owner"))
    owner_phone = clean(seller.get("phone") or seller.get("mobile") or case_data.get("phone"))
    owner_address = clean(seller.get("contact_address") or seller.get("address") or case_data.get("case_address"))

    title = clean(case_data.get("property_title") or case_data.get("ai_sales_title"))
    if not title and case_data.get("case_address"):
        title = clean(case_data.get("case_address"))[-12:]
    community = clean(case_data.get("community_name"))

    ptype = infer_property_type(case_data, seller)
    main_use = clean(case_data.get("deed_main_use"))
    material = clean(case_data.get("deed_main_material"))
    price = clean(case_data.get("case_price") or seller.get("expected_price"))

    deal_type = clean(seller.get("deal_type") or case_data.get("deal_type") or "sale").lower()
    is_rent = deal_type in {"rent", "出租", "租"}
    is_sale = not is_rent

    is_apartment = "公寓" in ptype
    is_huaxia = "華廈" in ptype or "華夏" in ptype
    is_toutian = "透天" in ptype
    is_villa = "別墅" in ptype
    is_farm = "農舍" in ptype
    is_store = "店" in ptype
    is_suite = "套" in ptype
    is_factory = "廠" in ptype or "工業" in main_use

    use_res = ("住宅" in main_use) or ("住家" in main_use)
    use_store = "店" in main_use
    use_parking = "停車" in main_use or "車庫" in main_use
    use_factory = "工業" in main_use or "廠" in main_use
    use_commercial = "商業" in main_use
    use_office = "辦公" in main_use or "事務所" in main_use

    structure_rc = "鋼筋混凝土" in material or "RC" in material.upper()
    structure_src = "鋼骨" in material or "SRC" in material.upper()
    structure_reinforced_brick = "加強磚" in material
    structure_brick = ("磚" in material and not structure_reinforced_brick)
    structure_other = bool(material and not any([structure_rc, structure_src, structure_reinforced_brick, structure_brick]))

    layout = clean(case_data.get("layout"))
    room = hall = bath = ""
    m = re.search(r"(\d+)\s*房", layout)
    if m: room = m.group(1)
    m = re.search(r"(\d+)\s*[廳厅]", layout)
    if m: hall = m.group(1)
    m = re.search(r"(\d+)\s*衛", layout)
    if m: bath = m.group(1)

    completed_y = clean(case_data.get("completed_minguo_year"))
    completed_m = clean(case_data.get("completed_month"))
    completed_d = clean(case_data.get("completed_day"))
    if not completed_y and case_data.get("deed_completed_date"):
        m = re.search(r"民國\s*(\d+)\s*年\s*(\d+)\s*月\s*(\d+)\s*日", clean(case_data.get("deed_completed_date")))
        if m:
            completed_y, completed_m, completed_d = m.group(1), m.group(2), m.group(3)

    feature = clean(case_data.get("ai_feature_note") or case_data.get("property_highlight_note") or case_data.get("life_note"))
    if not feature:
        feature = "、".join([x for x in [
            clean(case_data.get("case_address")),
            f"建物約{_fmt(case_data.get('total_ping'))}坪" if clean(case_data.get("total_ping")) else "",
            f"土地約{_fmt(case_data.get('land_ping'))}坪" if clean(case_data.get("land_ping")) else "",
            clean(case_data.get("deed_main_use")),
            clean(case_data.get("deed_main_material")),
        ] if x])

    special = clean(case_data.get("deed_mortgage_note") or case_data.get("case_note"))
    feature_short = feature[:105]
    special_short = special[:130]

    return {
        "委託類別": (
            f"委託類別：{checkbox(is_sale)}出售{checkbox(is_rent)}出租    委託類別：□專任□一般    案件編號：□□□□□□□□□",
            16, None
        ),
        "客戶來源": (
            "客戶來源：□踩線 □來店 □來電 ■謄本開發 □親友 □其他 (說明)",
            16, None
        ),
        "姓名": (
            f"姓名：{owner_name}    身份字號：            性別：□男□女    出生日期：    年    月    日",
            16, None
        ),
        "住宅電話": (
            f"住宅電話：            公司電話：            行動電話：{owner_phone}    地址：{owner_address}",
            15, None
        ),
        "物件名稱": (
            f"物件名稱：【{title}】不超過 12 個中文字    社區名稱：【{community}】"
            f"案件型態：{checkbox(is_apartment)}公寓{checkbox(is_huaxia)}華廈{checkbox(is_toutian)}透天"
            f"{checkbox(is_villa)}別墅{checkbox(is_farm)}農舍{checkbox(is_store)}店面{checkbox(is_suite)}套房{checkbox(is_factory)}廠房",
            15, None
        ),
        "土地型態": (
            "土地型態：□建地□商業用地□工業用地□農業用地□保護地□其他用地    採光：【】面"
            "現 況：□空屋□自用□出租□結構體□空地□其他    面寬：【】米    深度：【】米",
            15, None
        ),
        "售屋動機": (
            f"售屋動機：□換屋□工作□就學□移民□資金運用□其他    土地定位路口名稱: "
            f"物件地址：{clean(case_data.get('case_address') or owner_address)}",
            15, None
        ),
        "地上樓層": (
            f"地上樓層：【{clean(case_data.get('floor_total'))}】地下層數：【】    建物方位：朝【{clean(case_data.get('facing'))}】"
            f" 大樓：朝【】陽台：朝【】所在樓別：【{clean(case_data.get('floor'))}】-【】"
            f" 物件格局：【{room}】房【{hall}】廰【{bath}】衛【】陽台【】廚房",
            15, None
        ),
        "竣工日期": (
            f"竣工日期：民國【{completed_y}】年【{completed_m}】月【{completed_d}】日"
            f" 屋齡：【{clean(case_data.get('building_age'))}】年 預計完工：民國【】年【】月【】日",
            15, None
        ),
        "外壁材質": (
            f"外壁材質：□洗石子 □馬賽克 □方塊磚□二丁掛 □玻璃帷幕□花崗石 □原木□其他"
            f"建物結構：{checkbox(structure_brick)}磚造 {checkbox(structure_reinforced_brick)}加強磚造 "
            f"{checkbox(structure_rc)}鋼筋混凝土 RC{checkbox(structure_src)}鋼骨鋼筋混凝土 SRC□石材 {checkbox(structure_other)}其他建材",
            15, None
        ),
        "面臨路寬": (
            "面臨路寬：【】米    中庭：□有    邊間：□是    出租說明：【】"
            "管理方式：□無 □保全公司 □管理員(警衛) □守望亭 □固定駐警 □巡守人員 □保全設施",
            15, None
        ),
        "管 理 費": (
            "管 理 費：【】元 內含清潔費：□有 清潔費：【】元    電梯數：【】 每層戶數：【】"
            "繳費方式：□月繳 □雙月繳 □季繳 □半年繳 □年繳 □其他",
            15, None
        ),
        "建物主要登記用途": (
            f"建物主要登記用途：{checkbox(use_res)}住家用 {checkbox(use_store)}店鋪 □國民住宅 "
            f"{checkbox(use_parking)}停車空間 {checkbox(use_factory)}工業用或廠房 {checkbox(use_commercial)}商業用 {checkbox(use_office)}辦公室",
            15, None
        ),
        "建物總面積": (
            f"建物總面積：【{_fmt(case_data.get('total_ping'))}】坪＝ 主建【{_fmt(case_data.get('main_ping'))}】坪"
            f"＋附屬【{_fmt(case_data.get('attached_ping'))}】坪＋公設【{_fmt(case_data.get('public_ping'))}】坪"
            f"＋車位【{_fmt(case_data.get('parking_ping'))}】坪"
            f"土地總面積：【{_fmt(case_data.get('land_ping'))}】坪＝ 基地面積：【{_fmt(case_data.get('land_ping'))}】坪"
            f"＋土地持分：【{_fmt(case_data.get('land_ping'))}】坪",
            14, None
        ),
        "售價": (
            f"售價：【{price}】萬    月租金：【】萬/月    押金：【】萬    押金月數：【】",
            15, None
        ),
        "小學學區": (
            "小學學區：【】 國中學區：【】 市場購物：【】公園綠地：【】 醫療機構：【】 鄰近車站：【】建設公司：【】 商圈名稱：【】",
            15, None
        ),
        "案件特色詳細說明": (
            f"案件特色詳細說明：{feature_short}\nPS.此整份煩請詳細填寫，謝謝。\n產權特別注意事項：{special_short}",
            14, None
        ),
    }


def fill_case_form_docx_bytes(template_docx: str | Path, case_data: dict[str, Any], seller: dict[str, Any] | None = None) -> bytes:
    template_docx = Path(template_docx)
    if not template_docx.exists():
        raise FileNotFoundError(f"找不到 Word 案件輸入表範本：{template_docx}")

    lines = build_case_form_lines(case_data, seller=seller)

    with zipfile.ZipFile(template_docx, "r") as zin:
        root = etree.fromstring(zin.read("word/document.xml"))

        set_all_fonts_to_dfkai(root)

        for p in root.xpath(".//w:txbxContent//w:p", namespaces=NS):
            old = paragraph_text(p).strip()
            if not old:
                continue
            for prefix, (new_text, size_hp, bold) in lines.items():
                if old.startswith(prefix):
                    replace_paragraph_text(p, new_text, size_half_points=size_hp, bold=bold)
                    break

        for p in root.xpath(".//w:body/w:p", namespaces=NS):
            for rpr in p.xpath(".//w:rPr", namespaces=NS):
                _set_rpr_font(rpr)

        new_xml = etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True)

        out = BytesIO()
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zout:
            for item in zin.infolist():
                data = zin.read(item.filename)
                if item.filename == "word/document.xml":
                    data = new_xml
                zout.writestr(item, data)
        return out.getvalue()


def fill_case_form_docx_file(template_docx: str | Path, output_docx: str | Path, case_data: dict[str, Any], seller: dict[str, Any] | None = None) -> Path:
    output_docx = Path(output_docx)
    output_docx.parent.mkdir(parents=True, exist_ok=True)
    output_docx.write_bytes(fill_case_form_docx_bytes(template_docx, case_data, seller=seller))
    return output_docx


def parse_deed_pdf_to_docx(deed_pdf: str | Path, template_docx: str | Path, output_docx: str | Path, seller: dict[str, Any] | None = None, extra_case_data: dict[str, Any] | None = None):
    from deed_case_form_tool import parse_pdf_to_case_data
    parsed = parse_pdf_to_case_data(deed_pdf)
    case_data = dict(parsed.get("case_data") or {})
    if extra_case_data:
        case_data.update({k: v for k, v in extra_case_data.items() if clean(v)})
    fill_case_form_docx_file(template_docx, output_docx, case_data, seller=seller)
    return output_docx


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="謄本 PDF → Word 案件輸入表（標楷體、直接 key 入 Word）")
    parser.add_argument("deed_pdf")
    parser.add_argument("template_docx")
    parser.add_argument("output_docx")
    parser.add_argument("--seller-name", default="")
    parser.add_argument("--seller-phone", default="")
    parser.add_argument("--price", default="")
    parser.add_argument("--property-title", default="")
    parser.add_argument("--property-type", default="透天")
    args = parser.parse_args()

    extra = {
        "case_price": args.price,
        "property_title": args.property_title,
    }
    seller = {
        "name": args.seller_name,
        "phone": args.seller_phone,
        "deal_type": "sale",
        "property_type": args.property_type,
    }
    parse_deed_pdf_to_docx(args.deed_pdf, args.template_docx, args.output_docx, seller=seller, extra_case_data=extra)
    print(f"已產生：{args.output_docx}")
