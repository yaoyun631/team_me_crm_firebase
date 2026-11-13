"""Team ME 案件輸入表 V2.3：同一草稿整合多張建坪截圖與房仲文案。
在主程式最末尾 import register_property_case_v1 並註冊。
"""
import os, re, json, base64, tempfile
from io import BytesIO
from datetime import datetime
import requests
from flask import Blueprint, request, session, redirect, url_for, abort, send_file, render_template_string, current_app
from google.cloud import firestore
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired

FIELDS = {
 'address':'物件地址','building_no':'建號','land_no':'地號','property_name':'物件名稱','community':'社區名稱',
 'total_ping':'建物總面積','main_ping':'主建','aux_ping':'附屬','shared_ping':'公設','parking_ping':'車位坪數',
 'land_ping':'土地總面積','land_section':'地段','land_zoning':'使用分區','land_subzoning':'子分區','building_coverage':'建蔽率','floor_area_ratio':'容積率','building_footprint_ping':'建築面積','allowable_building_ping':'可用建坪','land_assessed_value':'公告現值','land_announced_price':'公告地價','age':'屋齡','floor':'所在樓層','total_floors':'地上樓層','basement_floors':'地下層數',
 'completed_date':'竣工日期','structure':'建物結構','rooms':'房','halls':'廳','baths':'衛','balconies':'陽台',
 'kitchens':'廚房','price':'售價','rent':'月租金','parking_type':'停車方式','management_fee':'管理費',
 'owner_name':'姓名','owner_phone':'行動電話','listing_type':'委託類別','contract_type':'專任／一般',
 'property_type':'案件型態','features':'案件特色詳細說明','notes':'產權特別注意事項'
}
FIELDS.update({'agent': '承辦人', 'points': '點數分配', 'case_no': '案件編號', 'key_location': '鑰匙位置', 'showing_method': '帶看方式', 'sale_reason': '售屋動機', 'customer_source': '客戶來源', 'no_web': '不上網', 'property_form': '案件型式', 'land_type': '土地型態', 'occupancy': '現況', 'lighting': '採光面數', 'frontage': '面寬', 'depth': '深度', 'road_width': '路寬', 'sitting_direction': '坐向', 'facing_direction': '朝向', 'elevator_count': '電梯數', 'units_per_floor': '每層戶數', 'floor_from': '所在樓別起', 'floor_to': '所在樓別迄', 'min_price': '底價', 'rent_identity': '租客身分要求', 'rent_gender': '租客性別要求', 'rent_rules': '租屋守則', 'rent_equipment': '房東提供設備'})

PROMPT = '''你是台灣房仲案件輸入表辨識助手。從圖片擷取明確看得到的資訊，輸出純 JSON object，key 僅可用以下名稱：
''' + ', '.join(FIELDS.keys()) + '''。
所有值必須是字串，坪數只留數字，日期用 YYYY-MM-DD，無法確認的欄位不要輸出。不可猜測。土地資料截圖中「地坪」對應 land_ping；「建築面積」對應 building_footprint_ping；「可用建坪」對應 allowable_building_ping，不可誤當建物總面積。地段對應 land_section，地號對應 land_no，多筆地號完整保留。土地持分不明時不得推測。截圖如有「車位：44.067坪」等異常，保留原值但在 _warnings 陣列說明疑慮；_warnings 可為字串陣列。圖片中建號、地號可以擷取但不應硬填入原PDF沒有的欄位。租屋守則與設備等多選資料請以頓號、逗號分隔。'''

def parse_image(image_bytes, mime='image/jpeg'):
    key = os.getenv('GEMINI_API_KEY') or os.getenv('GOOGLE_API_KEY')
    if not key: raise RuntimeError('未設定 GEMINI_API_KEY（或 GOOGLE_API_KEY）')
    from google import genai
    from google.genai import types
    client = genai.Client(api_key=key)
    response = client.models.generate_content(model=os.getenv('PROPERTY_GEMINI_MODEL','gemini-2.5-flash'), contents=[PROMPT,types.Part.from_bytes(data=image_bytes,mime_type=mime)], config=types.GenerateContentConfig(response_mime_type='application/json'))
    data=json.loads(response.text)
    return {k: str(v) for k,v in data.items() if k in FIELDS and v is not None}, data.get('_warnings', [])

def parse_text(s):
    """房仲新接文案/文字補充的離線解析：只擷取明確資訊，保留原始細節。"""
    t=(s or '').replace('＃','#').replace('臺','台').strip()
    t=re.sub(r'^#(?:補充案件|文案建檔)\s*', '', t)
    out={}
    lines=[x.strip() for x in t.splitlines() if x.strip()]
    address=re.search(r'(?:地址|物件地址)\s*[:：]\s*([^\n]+)',t)
    if address: out['address']=address.group(1).strip().replace('（','(').replace('）',')')
    else:
        m=re.search(r'台[^\n，,]{2,70}?\d+(?:-\d+)?號(?:[（(]\d+樓[）)])?',t)
        if m:out['address']=m.group(0)
    for pattern,key in [
      (r'(?:建坪|總坪|建物總面積)\s*[:：]?\s*([\d.]+)\s*坪','total_ping'),
      (r'(?:主坪|主建)\s*[:：]?\s*([\d.]+)\s*坪','main_ping'),
      (r'屋齡\s*[:：]?\s*([\d.]+)\s*年','age'),
      (r'管理費\s*[:：]?\s*\$?([\d,]+)\s*(?:元|/月|／月)','management_fee'),
      (r'(?:售價|開價|總價)\s*[:：]?\s*\$?([\d,.]+)\s*萬','price'),
      (r'(?:社區|社區名稱)\s*[:：]\s*([^\n，,]+)','community'),
      (r'(?:屋主姓名|屋主|姓名)\s*[:：]\s*([^\n，,]+)','owner_name'),
      (r'(?:手機|行動電話)\s*[:：]\s*([0-9-]+)','owner_phone'),
    ]:
        m=re.search(pattern,t)
        if m:out[key]=m.group(1).strip()
    # 支援「租1.4萬」、「租金：$12400-$13900」、「月租金：14000」；範圍不冒充單一租金。
    rent=re.search(r'(?:月租金|租金|(?<!出)租)\s*[:：]?\s*\$?\s*([\d,.]+)\s*(萬)?(?:\s*[-~～至到]\s*\$?\s*([\d,.]+)\s*(萬)?)?',t)
    if rent:
        first=float(rent.group(1).replace(',',''))*(10000 if rent.group(2) else 1)
        out['rent']=str(int(first)) if first.is_integer() else str(first)
        if rent.group(3):
            second=float(rent.group(3).replace(',',''))*(10000 if rent.group(4) else 1)
            if second!=first:out['_rent_range']=f'{int(first):,}～{int(second):,}元/月'
    m=re.search(r'(\d+)\s*房(?:\s*(\d+)\s*[廳厅])?(?:\s*(\d+)\s*衛)?',t)
    if m:
        out['rooms']=m.group(1)
        if m.group(2):out['halls']=m.group(2)
        if m.group(3):out['baths']=m.group(3)
    if '套房' in t and 'rooms' not in out:out['property_type']='套房'
    if '2房' in t and 'property_type' not in out:out['property_type']='華廈' if '華廈' in t else '套房' if '套房' in t else ''
    if not out.get('property_type'):out.pop('property_type',None)
    # 第一個有意義的標題；跳過「子玲 x 曜昀 新接」。
    title_lines=[x for x in lines if not re.search(r'新接|^#|^地址\s*[:：]',x)]
    if title_lines:
        title=title_lines[0].lstrip('❤️❤✨🏠🏡 ').strip()
        if not re.match(r'^(格局|租金|租期|押金|備註|車位|管理費|電|水)\s*[:：]',title):
            out['property_name']=title[:12]
    # V2.4：可直接從 LINE「#補充案件」的逐行鍵值文字更新新版 PDF 欄位。
    field_aliases = {label:key for key,label in FIELDS.items()}
    field_aliases.update({'委託類別':'listing_type','委託性質':'contract_type',
                          '建物方位':'facing_direction','坐向':'sitting_direction',
                          '所在樓層':'floor_from','地上樓層':'total_floors',
                          '房數':'rooms','廳數':'halls','衛數':'baths',
                          '租客身份要求':'rent_identity', '租客性別要求':'rent_gender',
                          '面積':'total_ping','屋主電話':'owner_phone'})
    for line in lines:
        m=re.match(r'^([^：:]{1,16})\s*[：:]\s*(.+)$',line)
        if not m:continue
        key=field_aliases.get(m.group(1).strip())
        if not key:continue
        value=m.group(2).strip()
        if key=='rent':
            n=re.search(r'[\d,.]+',value)
            if n:
                amount=float(n.group(0).replace(',',''))*(10000 if '萬' in value else 1)
                value=str(int(amount)) if amount.is_integer() else str(amount)
        out[key]=value
    if out.get('floor_from') and not out.get('floor'):out['floor']=out['floor_from']
    # 將原文中沒有專用欄位的資訊完整保留在備註，不遺失可寵、租補、帶看、垃圾車等細節。
    skip_prefix=('地址：','地址:')
    detail=[]
    for line in lines:
        if line.startswith(skip_prefix) or line.startswith('#') or '新接' in line:continue
        if re.fullmatch(r'[—─━\-_= ]{4,}',line):continue
        detail.append(line)
    if '_rent_range' in out:detail.insert(0,'租金範圍：'+out.pop('_rent_range'))
    if detail:out['features']='；'.join(detail)
    out['listing_type']='出租' if rent and '售價' not in t and '開價' not in t else out.get('listing_type','')
    return {k:v for k,v in out.items() if k in FIELDS and v not in ('',None)}

def split_property_address(raw):
    """把台灣門牌地址拆成表格各欄，移除已印刷的行政區與地址單位。

    不猜測缺少的行政區或門牌；未能確認的部分保持空白。
    """
    address = re.sub(r'\s+', '', str(raw or '').replace('臺', '台'))
    address = address.replace('（', '(').replace('）', ')')
    # 文案常見「（1樓）」及「(1樓)」
    address = address.replace('(', '').replace(')', '')
    parts = {k: '' for k in ('city','district','road','section','lane','alley','number','floor')}
    if not address:
        return parts
    # 縣市名稱去除最後一個「縣」或「市」；行政區名稱去除「區／鄉／鎮／市」。
    m = re.match(r'^(?P<city>[^縣市]{1,5}[縣市])', address)
    if m:
        parts['city'] = m.group('city')[:-1]
        address = address[m.end():]
    m = re.match(r'^(?P<district>[^區鄉鎮市]{1,6}[區鄉鎮市])', address)
    if m:
        parts['district'] = m.group('district')[:-1]
        address = address[m.end():]
    # 路名包含「台灣大道」等，並不強制一定是兩字路名。
    m = re.match(r'^(?P<road>[^路街巷弄號樓]{1,25}?)(?:路|街|大道)', address)
    if m:
        parts['road'] = m.group('road')
        address = address[m.end():]
    # 段可以是中文數字，例如「一段／七段」，也可能是阿拉伯數字。
    for key, pattern in (
        ('section', r'^([零〇一二三四五六七八九十百\d]+)段'),
        ('lane', r'^([\d]+(?:之\d+)?)巷'),
        ('alley', r'^([\d]+(?:之\d+)?)弄'),
        ('number', r'^([\d]+(?:[-－之]\d+)*)號'),
        ('floor', r'^(?:地下)?([\d]+(?:之\d+)?)樓(?:之([\d]+))?'),
    ):
        m = re.match(pattern, address)
        if m:
            parts[key] = m.group(1)
            if key == 'floor' and m.lastindex and m.lastindex > 1 and m.group(2):
                parts[key] += '之' + m.group(2)
            address = address[m.end():]
    return parts


def register_property_case_v1(app, db, pdf_template, access_token):
    import fitz
    bp=Blueprint('property_v1',__name__)
    def signer():
        return URLSafeTimedSerializer(app.secret_key, salt='property-case-line-v17')
    def share_url(cid,kind):
        token=signer().dumps({'cid':cid,'kind':kind})
        # LINE 伺服器必須能以公開 HTTPS 直接讀取圖片，不能走後台登入。
        base=os.getenv('PUBLIC_BASE_URL','https://teamme.onrender.com').rstrip('/')
        return f'{base}/property-case/line-share/{cid}/{kind}/{token}'
    def render_pdf_bytes(cid):
        doc=db.collection('property_cases').document(cid).get()
        if not doc.exists: abort(404)
        fields=(doc.to_dict() or {}).get('fields',{})
        # Prefer configured path; tolerate a templates/ deployment layout.
        # A missing PDF must be diagnosed before generating a LINE image URL.
        candidates = [
            pdf_template,
            os.path.join(os.path.dirname(os.path.abspath(__file__)), '案件輸入表-115.11.06(新版).pdf'),
            os.path.join(os.path.dirname(os.path.abspath(__file__)), 'templates', '案件輸入表-115.11.06(新版).pdf'),
        ]
        resolved_template = next((p for p in candidates if p and os.path.isfile(p)), None)
        if not resolved_template:
            app.logger.error('PROPERTY PDF TEMPLATE MISSING; checked: %s', candidates)
            abort(503, description='案件輸入表範本未部署，請確認 PDF 已提交至 Render 專案')
        src=fitz.open(resolved_template);page=src[0]
        # V1.7: A4 原版固定欄位座標。PDF points; 原版 595.32 x 841.92。
        # 不再 search_for(標籤) 後插字，避免落在「【」外、跨欄及重疊。
        font_path=os.getenv('PROPERTY_CJK_FONT','/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc')
        font='china-s'
        if os.path.isfile(font_path):
            try: page.insert_font(fontname='casefont',fontfile=font_path);font='casefont'
            except Exception: app.logger.warning('Cannot load CJK font: %s',font_path)
        # V2.0: 使用實際字寬測量，單行欄位水平、垂直置中。
        # 不再依賴 insert_textbox 自動換行（可能跨出印刷方框）。
        try:
            measure_font = fitz.Font(fontfile=font_path) if font == 'casefont' else fitz.Font('china-s')
        except Exception:
            measure_font = fitz.Font('china-s')

        def put(key, rect, size=8, max_chars=None, val=None):
            value=str(fields.get(key,'') if val is None else val).strip()
            if not value:return
            if max_chars:value=value[:max_chars]
            r=fitz.Rect(*rect)
            # 兩側及上下留白，避免貼到【】或格線。
            inner=fitz.Rect(r.x0+1.5,r.y0+1.0,r.x1-1.5,r.y1-1.0)
            if inner.width<=2 or inner.height<=2:return
            is_long=key in ('features','notes')
            if is_long:
                # 長備註限制在指定區域內；無法放入的部分不繪製，以免蓋住下一列。
                for fs in (size,6.5,6,5.5,5):
                    if fs<=0:continue
                    for length in (len(value), int(len(value)*.8), int(len(value)*.6), int(len(value)*.4)):
                        text=value[:max(1,length)]
                        if len(text)<len(value):text=text.rstrip('；，、 ')+ '…'
                        shape=page.new_shape()
                        remain=shape.insert_textbox(inner,text,fontname=font,fontsize=fs,
                                                    color=(0.04,0.18,0.5),lineheight=1.12)
                        if remain>=0:
                            shape.commit(overlay=True)
                            if len(text)<len(value):
                                app.logger.warning('PROPERTY NOTES TRUNCATED: %s',key)
                            return
                app.logger.warning('PROPERTY NOTES OVERFLOW: %s',key)
                return
            # V2.3：安全排版。原 PDF 的字體與線條絕不修改。
            # 括號區域內縮，避免字型筆畫超出測量寬度造成壓框。
            # 對窄欄位先使用左側定位，放不下再縮字；短數字則置中。
            padding_left=2.0
            padding_right=5.5 if r.width < 38 else 3.0
            safe=fitz.Rect(r.x0+padding_left,r.y0+1.5,
                           r.x1-padding_right,r.y1-1.5)
            if safe.width <= 2 or safe.height <= 2:
                return
            fs=float(size)
            def metrics(fontsize):
                return (measure_font.text_length(value,fontsize=fontsize),
                        (measure_font.ascender-measure_font.descender)*fontsize)
            # 實際字寬額外保留 12% 緩衝，避免渲染字型與量測字型不同。
            # 先以原字級計算，必要時以 0.2pt 步進縮小。
            while fs >= 3.8:
                width,height=metrics(fs)
                if width*1.12 <= safe.width and height <= safe.height:
                    break
                fs=round(fs-0.2,2)
            if fs < 3.8:
                app.logger.warning('PROPERTY FIELD SKIPPED (no safe fit): %s=%s',key,value[:60])
                return
            width,height=metrics(fs)
            # 寬度達欄位 70% 以上時偏左；否則以框內中心置中。
            x=safe.x0 if width*1.12 > safe.width*0.70 else safe.x0+(safe.width-width)/2
            # 二次校驗實際文字右界；不足則往左移，仍不足就不畫。
            x=min(x,safe.x1-width*1.06)
            x=max(safe.x0,x)
            if x+width*1.06 > safe.x1+0.01:
                app.logger.warning('PROPERTY FIELD OVERFLOW BLOCKED: %s=%s',key,value[:60])
                return
            baseline=safe.y0+(safe.height-height)/2+measure_font.ascender*fs
            page.insert_text((x,baseline),value,fontname=font,fontsize=fs,
                             color=(0.04,0.18,0.5),overlay=True)
        # 新版 115.11.06 的欄位座標：只在方括號內印字，不修改 PDF 原表。
        cells = {
          'property_name':(89,135,283,148), 'community':(460,135,562,148),
          'lighting':(67,212,95,226),'frontage':(193,212,221,226),
          'depth':(301,212,329,226),'road_width':(403,212,431,226),
          'total_floors':(91,228,107,241),'basement_floors':(187,228,203,241),
          'sitting_direction':(313,228,329,241),'facing_direction':(367,228,383,241),
          'elevator_count':(451,228,461,241),'units_per_floor':(547,228,563,241),
          'floor_from':(91,244,107,258),'floor_to':(139,244,155,258),
          'rooms':(257,244,273,258),'halls':(311,244,327,258),
          'baths':(365,244,381,258),'balconies':(419,244,435,258),
          'kitchens':(485,244,501,258),
          'agent':(427,24,460,37),'points':(455,10,475,22),
          'case_no':(408,44,560,59),
          'sale_reason':(101,96,198,109),'min_price':(65,294,540,307),
          'features':(116,337,567,405),'notes':(115,409,566,436),
          'land_zoning':(105,262,350,275),
          'building_coverage':(65,278,115,289),'floor_area_ratio':(161,278,204,289),
        }
        # 舊欄位保留 Firestore，但新版 PDF 沒有主建、公設、建坪、售價等格子，不強塞。
        if not fields.get('floor_from') and fields.get('floor'):
            cells['floor'] = cells['floor_from']
        for key,rect in cells.items():
            value=fields.get(key)
            if key=='case_no' and value: value=str(value)[:9]
            if value: put(key,rect,size=8 if key not in ('features','notes') else 7,val=value)

        # 勾選：尋找新版 PDF 印刷文字中的方框，在框正中央寫入 ✓。
        # 以字元實際 bbox 追蹤，不假定不同欄位有相同 x。
        def checked(value, label):
            if isinstance(value, (list,tuple,set)): return label in value
            x=str(value or '').strip()
            return label in [v.strip() for v in re.split(r'[,，、;；/\n]+',x)] or x==label
        def mark_checkbox(label, occurrence=0):
            targets=[]
            for block in page.get_text('rawdict')['blocks']:
                for line in block.get('lines',[]):
                    chars=[c for span in line.get('spans',[]) for c in span.get('chars',[])]
                    value=''.join(c['c'] for c in chars)
                    for match in re.finditer('□'+re.escape(label),value):
                        targets.append(chars[match.start()]['bbox'])
            if occurrence>=len(targets):return
            x0,y0,x1,y1=targets[occurrence]
            page.insert_text((x0+0.5,y1-1.3),'X',fontname=font,fontsize=8,color=(.04,.18,.5),overlay=True)
        def mark_options(key, options, occurrence=0):
            for option in options:
                if checked(fields.get(key),option):mark_checkbox(option,occurrence)
        # 出售/出租與專任/一般在同一列；使用標籤辨識正確方框。
        mark_options('listing_type',['出售','出租'])
        mark_options('contract_type',['專任','一般'])
        mark_options('key_location',['1 店','2 店','春天','幸福領航店','4 店','5 店','7 店'])
        mark_options('showing_method',['洽專員','KEY','管理室'])
        mark_options('customer_source',['踩線','來店','來電','謄本開發','親友'])
        mark_options('property_type',['公寓','華廈','大樓','透天','別墅','土地'])
        mark_options('property_form',['住家','店面','套房','辦公','廠辦','廠房','農舍'])
        mark_options('land_type',['建地','商業用地','工業用地','農業用地','保護地','其他用地'])
        mark_options('occupancy',['空屋','自用','出租','結構體','空地'])
        mark_options('no_web',['不上網請勾選'])
        mark_options('rent_rules',['開伙','飼養寵物','房東同住','可短期租賃','可供奉神明','可入籍'])
        equipment=['網路','第四台','洗衣機','脫水機','濾水器','傳統電視','液晶電視','烘衣機','飲水機','冰箱','瓦斯爐','微波爐','冷氣','電話','烤箱','電熱水器','天然瓦斯','電磁爐','電風扇','中央空調','排油煙機','衣櫥','餐桌','餐椅','單人床','雙人床','書桌','茶几','置物櫃','書架','椅子','單人沙發','雙人（以上）沙發','窗簾','流理台','電梯','公用陽台','休閒設施','滅火器','緩降梯','緊急照明','監視系統']
        mark_options('rent_equipment',equipment)
        # 租客身分/性別是圓形選項，不對其他欄位誤勾。
        def mark_circle(label, y_min, y_max):
            for block in page.get_text('rawdict')['blocks']:
                for line in block.get('lines',[]):
                    chars=[c for span in line.get('spans',[]) for c in span.get('chars',[])]
                    value=''.join(c['c'] for c in chars)
                    for m in re.finditer('○'+re.escape(label),value):
                        x0,y0,x1,y1=chars[m.start()]['bbox']
                        if y_min<=y0<=y_max:
                            page.insert_text((x0+.5,y1-1.4),'X',fontname=font,fontsize=8,color=(.04,.18,.5))
                            return
        if fields.get('rent_identity'):mark_circle(str(fields['rent_identity']),470,487)
        if fields.get('rent_gender'):mark_circle(str(fields['rent_gender']),487,500)
        # 其他沒有固定方框的長文字只保留在案件資料庫，避免破壞原稿。
        out=src.tobytes(garbage=4,deflate=True);src.close()
        return out
    @bp.route('/property-case/line-share/<cid>/<kind>/<token>', methods=['GET','HEAD'])
    def line_share(cid,kind,token):
        # JPG 專供 LINE Messaging API，原圖和縮圖分別產生。
        if kind not in ('pdf','png','jpg','preview'): abort(404)
        try:
            data=signer().loads(token,max_age=86400 if kind in ('jpg','preview') else 1800)
            if data.get('cid')!=cid or data.get('kind')!=kind: abort(403)
        except (BadSignature,SignatureExpired): abort(403)
        pdfbytes=render_pdf_bytes(cid)
        if kind=='pdf':
            return send_file(BytesIO(pdfbytes),mimetype='application/pdf',as_attachment=True,download_name=f'案件輸入表_{cid[:8]}.pdf',max_age=0)
        doc=fitz.open(stream=pdfbytes,filetype='pdf')
        try:
            # A4 300DPI 2480x3508，縮圖約 600px 寬。
            dpi=300 if kind=='jpg' else 72 if kind=='preview' else 122
            pix=doc[0].get_pixmap(dpi=dpi,alpha=False,colorspace=fitz.csRGB)
            if kind in ('jpg','preview'):
                from PIL import Image
                im=Image.frombytes('RGB',(pix.width,pix.height),pix.samples)
                if kind=='preview':
                    im.thumbnail((600,850),Image.Resampling.LANCZOS)
                out=BytesIO()
                im.save(out,format='JPEG',quality=86 if kind=='jpg' else 75,optimize=True)
                blob=out.getvalue()
                if len(blob)>10*1024*1024: abort(500,'A4 圖片超過 LINE 10MB 限制')
                if kind=='preview' and len(blob)>1024*1024: abort(500,'縮圖超過 LINE 1MB 限制')
                mime='image/jpeg'
            else:
                blob=pix.tobytes('png');mime='image/png'
        finally:
            doc.close()
        response=send_file(BytesIO(blob),mimetype=mime,as_attachment=False,max_age=300)
        response.headers['X-Content-Type-Options']='nosniff'
        response.headers['Cache-Control']='public, max-age=300'
        return response

    def current(user):
        snap=db.collection('property_case_sessions').document(user).get()
        data=snap.to_dict() or {} if snap.exists else {}
        return data.get('case_id')

    # 建坪來源優先截圖；租賃與廣告條件優先文字。手動 #補充案件 優先於兩者。
    IMAGE_FIELDS={'total_ping','main_ping','aux_ping','shared_ping','parking_ping',
                  'land_ping','land_section','land_zoning','land_subzoning','building_coverage','floor_area_ratio','building_footprint_ping','allowable_building_ping','land_assessed_value','land_announced_price','age','completed_date','structure','total_floors',
                  'basement_floors','building_no','land_no'}
    TEXT_FIELDS={'property_name','community','rooms','halls','baths','balconies',
                 'kitchens','price','rent','management_fee','listing_type',
                 'property_type','parking_type','features','notes'}
    def save_case(cid, fields, user, warnings=None, source='text', raw_text=None):
        ref=db.collection('property_cases').document(cid)
        existing=ref.get().to_dict() or {}
        merged=dict(existing.get('fields') or {})
        sources=dict(existing.get('field_sources') or {})
        conflicts=list(existing.get('conflicts') or [])
        new_warnings=list(existing.get('warnings') or [])
        for warning in (warnings or []):
            if str(warning) not in new_warnings:new_warnings.append(str(warning))
        for key,value in (fields or {}).items():
            if key not in FIELDS or value is None or str(value).strip()=='':continue
            value=str(value).strip()
            old=merged.get(key)
            previous=sources.get(key,'')
            if old is not None and str(old).strip() and str(old).strip()!=value:
                # 同一來源的更新取最新值；不同來源依欄位特性決定。
                if source=='manual': accept=True
                elif previous=='manual':accept=False
                elif key in IMAGE_FIELDS:accept=source=='image' or previous!='image'
                elif key in TEXT_FIELDS:accept=source=='text' or previous!='text'
                else:accept=previous not in ('image','text') or source==previous
                if key in ('address','floor','total_floors','rooms','rent','total_ping','main_ping','parking_ping'):
                    issue=f'{FIELDS[key]}：{previous or "既有"}「{old}」與{source}「{value}」不同，保留「{value if accept else old}」；請確認'
                    if issue not in conflicts:conflicts.append(issue)
                if not accept:continue
            merged[key]=value
            sources[key]=source
        try:
            parking=float(str(merged.get('parking_ping','')).replace(',',''))
            total=float(str(merged.get('total_ping','')).replace(',',''))
            if total>0 and parking>total*0.5:
                note=f'車位坪數 {parking:g} 坪超過建物總坪數的一半，請核對原截圖欄位'
                if note not in new_warnings:new_warnings.append(note)
        except (ValueError,TypeError):pass
        # 不覆寫已收到的原始文案，便於事後核對及重新整理。
        history=list(existing.get('text_history') or [])
        if raw_text and str(raw_text).strip():
            history.append({'at':datetime.now().isoformat(),'text':str(raw_text)[:8000]})
            history=history[-12:]
        ref.set({'fields':merged,'field_sources':sources,'text_history':history,
                 'conflicts':conflicts[-30:],'warnings':new_warnings[-30:],
                 'updated_at':datetime.now().isoformat(),'owner_line_id':user,
                 'status':'草稿'},merge=True)
        return merged

    def report(cid):
        data=db.collection('property_cases').document(cid).get().to_dict() or {}
        fields=data.get('fields') or {}
        lines=[f'{FIELDS[k]}：{v}' for k,v in fields.items() if k in FIELDS and k not in ('features','notes')]
        if fields.get('features'):lines.append('特色：'+str(fields['features'])[:450])
        problems=(data.get('conflicts') or [])[-5:]
        warnings=(data.get('warnings') or [])[-3:]
        if problems:lines.append('\n⚠️ 資料差異（請核對）：\n'+'\n'.join(problems))
        if warnings:lines.append('\n⚠️ 辨識提醒：\n'+'\n'.join(str(x) for x in warnings))
        return ('📋 案件整合資料\n'+'\n'.join(lines or ['尚無資料'])+'\n\n可繼續傳建坪截圖或完整文案，最後輸入 #案件圖片。')[:4800]
    def handle(event):
        msg=event.get('message') or {}; typ=msg.get('type'); user=(event.get('source') or {}).get('userId','')
        if not user: return None
        raw=(msg.get('text') or '').strip()
        if typ=='text' and raw in ('#新增案件','新增案件'):
            ref=db.collection('property_cases').document()
            ref.set({'fields':{},'owner_line_id':user,'status':'草稿','created_at':datetime.now().isoformat(),'updated_at':datetime.now().isoformat()})
            db.collection('property_case_sessions').document(user).set({'user_id':user,'case_id':ref.id})
            return {'handled':True,'ok':True,'reply_text':'✅ 已建立案件草稿\n請直接傳送物件資料截圖（可多張），或文字補充。\n輸入 #案件資料 查看，#案件PDF 產生表格。'}
        if typ=='text' and raw.startswith('#文案建檔'):
            body=raw[len('#文案建檔'):].strip()
            if not body:return {'handled':True,'ok':False,'reply_text':'請在 #文案建檔 後貼上完整房仲新接文案。'}
            fields=parse_text(body)
            if not fields:return {'handled':True,'ok':False,'reply_text':'文案沒有辨識到可建檔資訊，請補充地址或租金。'}
            ref=db.collection('property_cases').document()
            ref.set({'fields':{},'owner_line_id':user,'status':'草稿','created_at':datetime.now().isoformat()})
            db.collection('property_case_sessions').document(user).set({'user_id':user,'case_id':ref.id})
            save_case(ref.id,fields,user,source="text",raw_text=body)
            return {'handled':True,'ok':True,'reply_text':'✅ 文案已建立案件草稿\n'+'\n'.join(f'{FIELDS[k]}：{str(v)[:110]}' for k,v in fields.items() if k!='features')+'\n\n輸入 #案件圖片 可產生 A4 表單。'}
        cid=current(user)
        if not cid: return None
        if typ=='text' and raw in ('#案件資料','#案件PDF','#案件圖片','#完成案件'):
            doc=db.collection('property_cases').document(cid).get().to_dict() or {}
            fields=doc.get('fields') or {}
            if raw=='#案件資料':
                lines=[f'{FIELDS[k]}：{v}' for k,v in fields.items() if k in FIELDS]
                return {'handled':True,'ok':True,'reply_text':report(cid)}
            if raw=='#完成案件':
                db.collection('property_cases').document(cid).update({'status':'已確認'})
                return {'handled':True,'ok':True,'reply_text':'✅ 案件已標記確認。輸入 #案件PDF 可取得下載連結。'}
            original_url=share_url(cid,'jpg')
            preview_url=share_url(cid,'preview')
            return {'handled':True,'ok':True,'reply_messages':[
                {'type':'image','originalContentUrl':original_url,'previewImageUrl':preview_url},
                {'type':'text','text':'🖼️ A4 案件輸入表已產生（300 DPI JPG）。點上方圖片即可放大、儲存或轉傳。\n如顯示空白，請檢查 Render 是否可公開存取圖片網址及部署日誌。'}]}
        if typ=='image':
            mid=msg.get('id')
            try:
                r=requests.get(f'https://api-data.line.me/v2/bot/message/{mid}/content',headers={'Authorization':f'Bearer {access_token}'},timeout=18)
                r.raise_for_status()
                fields,warnings=parse_image(r.content,r.headers.get('Content-Type','image/jpeg').split(';')[0])
                save_case(cid,fields,user,warnings,source="image")
                summary='\n'.join(f'{FIELDS[k]}：{v}' for k,v in fields.items())
                return {'handled':True,'ok':True,'reply_text':'✅ 截圖辨識完成\n'+(summary or '沒有辨識出可確認的欄位')+('\n⚠️ '+str(warnings[0]) if warnings else '')+'\n\n可繼續傳截圖或文字補充。'}
            except Exception as e:
                app.logger.exception('property screenshot failed')
                err=str(e)
                if 'RESOURCE_EXHAUSTED' in err or '429' in err or '402' in err or 'credits are depleted' in err:
                    msg='⚠️ 圖片 AI 辨識額度已用完，目前無法自動讀取截圖。\n案件草稿仍在，請改傳「#補充案件 地址：...、租金：...」等文字；額度恢復後可重新傳圖片。'
                else:
                    msg='⚠️ 截圖辨識失敗，案件草稿已保留。請稍後重試或使用 #補充案件 文字輸入。'
                return {'handled':True,'ok':False,'reply_text':msg}
        if typ=='text' and (raw.startswith(('#補充案件','補充案件')) or (not raw.startswith('#') and any(w in raw for w in ('地址','租金','開價','售價','格局','管理費','房','屋齡','新接','可寵','押金','租期','水費','電費','帶看','總坪','主建','公設')))):
            fields=parse_text(raw.replace('#補充案件','',1).replace('補充案件','',1))
            if not fields: return {'handled':True,'ok':False,'reply_text':'未找到可辨識欄位，請使用「開價1588萬，3房2廳2衛」或「地址：...」等格式。'}
            save_case(cid,fields,user,source='manual' if raw.startswith(('#補充案件','補充案件')) else 'text',raw_text=raw)
            return {'handled':True,'ok':True,'reply_text':'✅ 已合併至同一案件草稿\n'+'\n'.join(f'{FIELDS[k]}：{v}' for k,v in fields.items())}
        return None
    @bp.route('/property-case/pdf/<cid>')
    def pdf(cid):
        if not session.get('user_id'): return redirect(url_for('login',next=request.path))
        pdfbytes=render_pdf_bytes(cid)
        return send_file(BytesIO(pdfbytes),mimetype='application/pdf',as_attachment=True,download_name=f'案件輸入表_{cid[:8]}.pdf')
    @bp.route('/property-case')
    def list_cases():
        if not session.get('user_id'): return redirect(url_for('login'))
        docs=list(db.collection('property_cases').limit(100).stream())
        rows=[{'id':d.id,**(d.to_dict() or {})} for d in docs]
        return render_template_string('''<meta charset="utf-8"><title>案件建檔</title><style>body{font-family:Arial,sans-serif;max-width:950px;margin:30px auto}table{width:100%;border-collapse:collapse}td,th{padding:12px;border-bottom:1px solid #ddd}</style><h2>案件輸入表管理（初版）</h2><table><tr><th>地址／案名</th><th>狀態</th><th>更新時間</th><th>PDF</th></tr>{% for c in rows %}<tr><td>{{ c.get('fields',{}).get('address') or c.get('fields',{}).get('property_name') or c.id[:8] }}</td><td>{{c.get('status','草稿')}}</td><td>{{c.get('updated_at','')}}</td><td><a href="{{url_for('property_v1.pdf',cid=c.id)}}">下載</a></td></tr>{% endfor %}</table>''',rows=rows)
    app.register_blueprint(bp)
    app.config["PROPERTY_CASE_V15_HANDLER"] = handle
    old=app.config.get('PROPERTY_V1_ORIGINAL_PROCESSOR')
    if old is None:
        old=app.view_functions.get('line_webhook')
    # webhook 處理函式本身不重寫：包裝其所使用的全域訊息處理器
    namespace=old.__globals__ if old else {}
    previous=namespace.get('process_line_message_event')
    if previous:
        def wrapped(event):
            result=handle(event)
            if result is not None: return result
            return previous(event)
        namespace['process_line_message_event']=wrapped
    return handle
