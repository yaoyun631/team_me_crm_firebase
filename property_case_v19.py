"""Team ME 案件輸入表 V1.9：同一草稿整合多張建坪截圖與房仲文案。
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
 'land_ping':'土地總面積','age':'屋齡','floor':'所在樓層','total_floors':'地上樓層','basement_floors':'地下層數',
 'completed_date':'竣工日期','structure':'建物結構','rooms':'房','halls':'廳','baths':'衛','balconies':'陽台',
 'kitchens':'廚房','price':'售價','rent':'月租金','parking_type':'停車方式','management_fee':'管理費',
 'owner_name':'姓名','owner_phone':'行動電話','listing_type':'委託類別','contract_type':'專任／一般',
 'property_type':'案件型態','features':'案件特色詳細說明','notes':'產權特別注意事項'
}
PROMPT = '''你是台灣房仲案件輸入表辨識助手。從圖片擷取明確看得到的資訊，輸出純 JSON object，key 僅可用以下名稱：
''' + ', '.join(FIELDS.keys()) + '''。
所有值必須是字串，坪數只留數字，日期用 YYYY-MM-DD，無法確認的欄位不要輸出。不可猜測。截圖如有「車位：44.067坪」等異常，保留原值但在 _warnings 陣列說明疑慮；_warnings 可為字串陣列。圖片中建號、地號可以擷取但不應硬填入原PDF沒有的欄位。'''

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
            os.path.join(os.path.dirname(os.path.abspath(__file__)), '案件輸入表-使用中.pdf'),
            os.path.join(os.path.dirname(os.path.abspath(__file__)), 'templates', '案件輸入表-使用中.pdf'),
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
        def put(key, rect, size=8, max_chars=None, val=None):
            value=str(fields.get(key,'') if val is None else val).strip()
            if not value:return
            if max_chars:value=value[:max_chars]
            r=fitz.Rect(*rect)
            # 動態縮字直到放進自己的欄位；不擠到相鄰欄位。
            for fs in (size, size-0.5, size-1, size-1.5, size-2, 5):
                if fs<5:continue
                shape=page.new_shape()
                remain=shape.insert_textbox(r,value,fontname=font,fontsize=fs,color=(0.04,0.18,0.5),lineheight=1.08)
                if remain>=0:
                    shape.commit(overlay=True)
                    return
            app.logger.warning('PROPERTY FIELD OVERFLOW: %s=%s',key,value[:40])
        # x0,y0,x1,y1 全部是原 PDF 頁面座標，與印刷欄位逐一對位。
        cells={
          'property_name':(88,208,286,222), 'community':(496,208,557,222),
          'total_floors':(88,302,102,316),'basement_floors':(182,302,198,316),
          'floor':(87,318,101,332),'rooms':(232,318,246,332),
          'halls':(280,318,294,332),'baths':(328,318,342,332),
          'balconies':(380,318,397,332),'kitchens':(443,318,457,332),
          'age':(298,333,306,348),'management_fee':(103,428,120,441),
          'total_ping':(100,574,132,588),'main_ping':(212,574,240,588),
          'aux_ping':(308,574,336,588),'shared_ping':(404,574,432,588),
          'parking_ping':(500,574,528,588),
          'land_ping':(100,592,132,605),
          'price':(64,610,96,623),'rent':(208,610,240,623),
          'features':(132,720,571,743),'notes':(132,775,571,799),
        }
        for key,rect in cells.items():
            val=fields.get(key)
            if key=='rent' and val:
                # 表格單位「萬/月」，文字資料若為元，需換算後再填。
                try:
                    n=float(str(val).replace(',','').replace('$',''))
                    val=('%g'%(n/10000)) if n>=1000 else ('%g'%n)
                except (ValueError,TypeError):pass
            put(key,rect,size=8 if key not in ('features','notes') else 7,max_chars=12 if key=='property_name' else None,val=val)
        # 竣工日分別寫入民國年、月、日的獨立方框。
        date=str(fields.get('completed_date','')).strip()
        mm=re.match(r'^(\d{4})[-/](\d{1,2})[-/](\d{1,2})',date)
        if mm:
            year,month,day=map(int,mm.groups())
            put('completed_date',(104,333,126,348),val=str(year-1911) if year>1911 else str(year))
            put('completed_date',(152,333,174,348),val=str(month))
            put('completed_date',(200,333,222,348),val=str(day))
        # 地址依市、區、路、段、巷、弄、號、樓拆開，避免蓋到印刷標籤。
        address=str(fields.get('address','')).strip().replace('臺','台')
        if address:
            city=re.search(r'(.{2,4}市)',address)
            district=re.search(r'([\u4e00-\u9fff]{1,4}區)',address)
            road_part=address[district.end():] if district else (address[city.end():] if city else address)
            road=re.search(r'([\u4e00-\u9fff0-9一二三四五六七八九十]+(?:路|街))',road_part)
            sec=re.search(r'(\d+)段',address)
            lane=re.search(r'(\d+)巷',address)
            alley=re.search(r'(\d+)弄',address)
            no=re.search(r'(\d+(?:-\d+)?)號',address)
            fl=re.search(r'(\d+(?:之\d+)?)樓',address)
            for rect,m in [((62,286,125,301),city),((152,286,191,301),district),((214,286,258,301),road),((318,286,342,301),sec),((374,286,396,301),lane),((425,286,444,301),alley),((471,286,492,301),no),((512,286,557,301),fl)]:
                if m:put('address',rect,size=7,val=m.group(1))
        # 使用固定欄位時不把沒有欄位的建號、地號硬塞到 PDF 內。
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
                  'land_ping','age','completed_date','structure','total_floors',
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
