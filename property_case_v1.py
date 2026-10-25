"""Team ME 案件輸入表 V1：LINE 截圖/文字 -> Gemini 擷取 -> Firestore -> 原版 PDF。
在主程式最末尾 import register_property_case_v1 並註冊。
"""
import os, re, json, base64, tempfile
from io import BytesIO
from datetime import datetime
import requests
from flask import Blueprint, request, session, redirect, url_for, abort, send_file, render_template_string
from google.cloud import firestore

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
    out={}
    for line in re.split(r'[\n,，;；]+',s):
        m=re.search(r'(?:開價|售價)\s*[:：]?\s*([\d.]+)\s*萬',line)
        if m: out['price']=m.group(1)
        m=re.search(r'(\d+)房\s*(\d+)廳\s*(\d+)衛',line)
        if m: out.update(zip(('rooms','halls','baths'),m.groups()))
        for label,key in [('地址','address'),('物件名稱','property_name'),('社區','community'),('屋主','owner_name'),('手機','owner_phone'),('特色','features'),('備註','notes'),('管理費','management_fee')]:
            m=re.search(re.escape(label)+r'\s*[:：]\s*(.+)',line)
            if m: out[key]=m.group(1).strip()
    return out

def register_property_case_v1(app, db, pdf_template, access_token):
    import fitz
    bp=Blueprint('property_v1',__name__)
    def current(user):
        docs=list(db.collection('property_case_sessions').where('user_id','==',user).limit(1).stream())
        if docs:
            data=docs[0].to_dict() or {}
            if data.get('case_id'): return data['case_id']
        return None
    def save_case(cid, fields, user, warnings=None):
        ref=db.collection('property_cases').document(cid)
        existing=ref.get().to_dict() or {}
        merged={**existing.get('fields',{}),**fields}
        ref.set({'fields':merged,'updated_at':datetime.now().isoformat(),'owner_line_id':user,'warnings':(existing.get('warnings') or [])+(warnings or []),'status':'草稿'},merge=True)
        return merged
    def handle(event):
        msg=event.get('message') or {}; typ=msg.get('type'); user=(event.get('source') or {}).get('userId','')
        if not user: return None
        raw=(msg.get('text') or '').strip()
        if typ=='text' and raw in ('#新增案件','新增案件'):
            ref=db.collection('property_cases').document()
            ref.set({'fields':{},'owner_line_id':user,'status':'草稿','created_at':datetime.now().isoformat(),'updated_at':datetime.now().isoformat()})
            db.collection('property_case_sessions').document(user).set({'user_id':user,'case_id':ref.id})
            return {'handled':True,'ok':True,'reply_text':'✅ 已建立案件草稿\n請直接傳送物件資料截圖（可多張），或文字補充。\n輸入 #案件資料 查看，#案件PDF 產生表格。'}
        cid=current(user)
        if not cid: return None
        if typ=='text' and raw in ('#案件資料','#案件PDF','#完成案件'):
            doc=db.collection('property_cases').document(cid).get().to_dict() or {}
            fields=doc.get('fields') or {}
            if raw=='#案件資料':
                lines=[f'{FIELDS[k]}：{v}' for k,v in fields.items() if k in FIELDS]
                return {'handled':True,'ok':True,'reply_text':'📋 案件草稿\n'+'\n'.join(lines or ['尚無資料'])+'\n\n輸入 #案件PDF 產生 PDF'}
            if raw=='#完成案件':
                db.collection('property_cases').document(cid).update({'status':'已確認'})
                return {'handled':True,'ok':True,'reply_text':'✅ 案件已標記確認。輸入 #案件PDF 可取得下載連結。'}
            base=os.getenv('PUBLIC_BASE_URL','').rstrip('/')
            if not base: base=request.host_url.rstrip('/')
            return {'handled':True,'ok':True,'reply_text':f'📄 PDF 下載：{base}/property-case/pdf/{cid}\n（需要登入後台並有案件存取權）'}
        if typ=='image':
            mid=msg.get('id')
            try:
                r=requests.get(f'https://api-data.line.me/v2/bot/message/{mid}/content',headers={'Authorization':f'Bearer {access_token}'},timeout=18)
                r.raise_for_status()
                fields,warnings=parse_image(r.content,r.headers.get('Content-Type','image/jpeg').split(';')[0])
                save_case(cid,fields,user,warnings)
                summary='\n'.join(f'{FIELDS[k]}：{v}' for k,v in fields.items())
                return {'handled':True,'ok':True,'reply_text':'✅ 截圖辨識完成\n'+(summary or '沒有辨識出可確認的欄位')+('\n⚠️ '+str(warnings[0]) if warnings else '')+'\n\n可繼續傳截圖或文字補充。'}
            except Exception as e:
                app.logger.exception('property screenshot failed')
                return {'handled':True,'ok':False,'reply_text':f'⚠️ 截圖處理失敗：{str(e)[:150]}'}
        if typ=='text' and raw.startswith(('#補充案件','補充案件')):
            fields=parse_text(raw.replace('#補充案件','',1).replace('補充案件','',1))
            if not fields: return {'handled':True,'ok':False,'reply_text':'未找到可辨識欄位，請使用「開價1588萬，3房2廳2衛」或「地址：...」等格式。'}
            save_case(cid,fields,user)
            return {'handled':True,'ok':True,'reply_text':'✅ 已更新\n'+'\n'.join(f'{FIELDS[k]}：{v}' for k,v in fields.items())}
        return None
    @bp.route('/property-case/pdf/<cid>')
    def pdf(cid):
        if not session.get('user_id'): return redirect(url_for('login',next=request.path))
        doc=db.collection('property_cases').document(cid).get()
        if not doc.exists: abort(404)
        # 初版：僅限後台已登入人員；部署前應再補細緻的案件權限規則
        fields=(doc.to_dict() or {}).get('fields',{})
        src=fitz.open(pdf_template); page=src[0]
        # 以原表欄位標籤位置為定位基準，避開原有文字
        label_map={'property_name':'物件名稱','community':'社區名稱','total_ping':'建物總面積','main_ping':'主建','aux_ping':'附屬','shared_ping':'公設','land_ping':'土地總面積','price':'售價','rent':'月租金','age':'屋齡','management_fee':'管 理 費','rooms':'物件格局','total_floors':'地上樓層','completed_date':'竣工日期','features':'案件特色詳細說明','notes':'產權特別注意事項'}
        font_path=os.getenv('PROPERTY_CJK_FONT','/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc')
        if os.path.exists(font_path): page.insert_font(fontname='casefont',fontfile=font_path); font='casefont'
        else: font='china-s'
        for key,label in label_map.items():
            value=str(fields.get(key,'')).strip()
            if not value: continue
            hits=page.search_for(label)
            if not hits: continue
            box=hits[0]; x=box.x1+5; y=box.y1-2
            # 欄位寬度依原表保守限制；太長則截斷並保留原資料庫全文
            if key in ('features','notes'): x=box.x0+2; y=box.y1+9; value=value[:90]
            elif key=='property_name': value=value[:12]
            else: value=value[:24]
            try: page.insert_text((x,y),value,fontname=font,fontsize=7,color=(0.05,0.2,0.65),overlay=True)
            except Exception: app.logger.exception('PDF field failed: %s',key)
        buf=BytesIO(src.tobytes(garbage=4,deflate=True));src.close();buf.seek(0)
        return send_file(buf,mimetype='application/pdf',as_attachment=True,download_name=f'案件輸入表_{cid[:8]}.pdf')
    @bp.route('/property-case')
    def list_cases():
        if not session.get('user_id'): return redirect(url_for('login'))
        docs=list(db.collection('property_cases').limit(100).stream())
        rows=[{'id':d.id,**(d.to_dict() or {})} for d in docs]
        return render_template_string('''<meta charset="utf-8"><title>案件建檔</title><style>body{font-family:Arial,sans-serif;max-width:950px;margin:30px auto}table{width:100%;border-collapse:collapse}td,th{padding:12px;border-bottom:1px solid #ddd}</style><h2>案件輸入表管理（初版）</h2><table><tr><th>地址／案名</th><th>狀態</th><th>更新時間</th><th>PDF</th></tr>{% for c in rows %}<tr><td>{{ c.get('fields',{}).get('address') or c.get('fields',{}).get('property_name') or c.id[:8] }}</td><td>{{c.get('status','草稿')}}</td><td>{{c.get('updated_at','')}}</td><td><a href="{{url_for('property_v1.pdf',cid=c.id)}}">下載</a></td></tr>{% endfor %}</table>''',rows=rows)
    app.register_blueprint(bp)
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
