"""Team ME 案件輸入表 V1：LINE 截圖/文字 -> Gemini 擷取 -> Firestore -> 原版 PDF。
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
    """不耗 AI 額度的房仲文字解析；只填明確出現的欄位。"""
    out={}
    t=(s or '').replace('＃','#')
    for pat,key in [
        (r'(?:物件地址|地址)\s*[:：]\s*([^\n，,]+)', 'address'),
        (r'(?:開價|售價|總價)\s*[:：]?\s*([\d,.]+)\s*萬','price'),
        (r'(?:月租金|租金|租金每月)\s*[:：]?\s*(?:[$＄])?\s*([\d,]+)','rent'),
        (r'(?:管理費)\s*[:：]?\s*(?:[$＄])?\s*([\d,]+)','management_fee'),
        (r'(?:押金)\s*[:：]?\s*([^\n，,]+)','deposit_note'),
        (r'(?:租期)\s*[:：]?\s*([^\n，,]+)','lease_note'),
        (r'(?:物件名稱|案名)\s*[:：]\s*([^\n，,]+)','property_name'),
        (r'(?:社區名稱|社區)\s*[:：]\s*([^\n，,]+)','community'),
        (r'(?:屋主|姓名)\s*[:：]\s*([^\n，,]+)','owner_name'),
        (r'(?:手機|行動電話)\s*[:：]\s*([0-9\-]+)','owner_phone'),
        (r'(?:備註|特色)\s*[:：]\s*([^\n]+)','features'),
        (r'(?:總坪|建坪)\s*[:：]?\s*([\d.]+)\s*坪','total_ping'),
        (r'(?:主坪|主建)\s*[:：]?\s*([\d.]+)\s*坪','main_ping'),
        (r'(?:屋齡)\s*[:：]?\s*([\d.]+)\s*年','age'),
    ]:
        m=re.search(pat,t)
        if m and key in FIELDS: out[key]=m.group(1).strip()
    m=re.search(r'(\d+)\s*房\s*(\d+)\s*[廳厅]\s*(\d+)\s*衛',t)
    if m: out.update(zip(('rooms','halls','baths'),m.groups()))
    m=re.search(r'(?:格局)\s*[:：]?\s*(\d+)\s*房',t)
    if m and 'rooms' not in out: out['rooms']=m.group(1)
    m=re.search(r'(?:押金)\s*[:：]?\s*(兩|二|2)\s*個月',t)
    if m: out['notes']=(out.get('notes','')+' 押金兩個月').strip()
    if '租期' in t:
        m=re.search(r'租期\s*[:：]?\s*([^\n，,]+)',t)
        if m: out['notes']=(out.get('notes','')+' 租期：'+m.group(1)).strip()
    m=re.search(r'(?:車位|停車)\s*[:：]?\s*([^\n，,]+)',t)
    if m: out['parking_type']=m.group(1).strip()[:40]
    # 沒有明確「地址：」時，允許直接擷取台灣門牌
    if 'address' not in out:
        m=re.search(r'(?:台|臺)[中北南東]市[^\n，,]{1,55}?\d+\s*號(?:\s*\d+\s*樓)?',t)
        if m: out['address']=m.group(0).strip()
    # 對於原始 PDF 沒有欄位的租期/押金，放入備註
    return out

def register_property_case_v1(app, db, pdf_template, access_token):
    import fitz
    bp=Blueprint('property_v1',__name__)
    def signer():
        return URLSafeTimedSerializer(app.secret_key, salt='property-case-line-v12')
    def share_url(cid,kind):
        token=signer().dumps({'cid':cid,'kind':kind})
        # LINE 伺服器必須能以公開 HTTPS 直接讀取圖片，不能走後台登入。
        base=os.getenv('PUBLIC_BASE_URL','https://teamme.onrender.com').rstrip('/')
        return f'{base}/property-case/line-share/{cid}/{kind}/{token}'
    def render_pdf_bytes(cid):
        doc=db.collection('property_cases').document(cid).get()
        if not doc.exists: abort(404)
        fields=(doc.to_dict() or {}).get('fields',{})
        src=fitz.open(pdf_template);page=src[0]
        label_map={'property_name':'物件名稱','community':'社區名稱','total_ping':'建物總面積','main_ping':'主建','aux_ping':'附屬','shared_ping':'公設','land_ping':'土地總面積','price':'售價','rent':'月租金','age':'屋齡','management_fee':'管 理 費','rooms':'物件格局','total_floors':'地上樓層','completed_date':'竣工日期','features':'案件特色詳細說明','notes':'產權特別注意事項'}
        font_path=os.getenv('PROPERTY_CJK_FONT','/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc')
        if os.path.exists(font_path):
            try: page.insert_font(fontname='casefont',fontfile=font_path); font='casefont'
            except Exception: font='china-s'
        else: font='china-s'
        for key,label in label_map.items():
            value=str(fields.get(key,'')).strip()
            if not value: continue
            hits=page.search_for(label)
            if not hits: continue
            box=hits[0];x=box.x1+5;y=box.y1-2
            if key in ('features','notes'): x=box.x0+2;y=box.y1+9;value=value[:90]
            elif key=='property_name':value=value[:12]
            else:value=value[:24]
            try:page.insert_text((x,y),value,fontname=font,fontsize=7,color=(0.05,0.2,0.65),overlay=True)
            except Exception:app.logger.exception('PDF field failed: %s',key)
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
        if typ=='text' and raw in ('#案件資料','#案件PDF','#案件圖片','#完成案件'):
            doc=db.collection('property_cases').document(cid).get().to_dict() or {}
            fields=doc.get('fields') or {}
            if raw=='#案件資料':
                lines=[f'{FIELDS[k]}：{v}' for k,v in fields.items() if k in FIELDS]
                return {'handled':True,'ok':True,'reply_text':'📋 案件草稿\n'+'\n'.join(lines or ['尚無資料'])+'\n\n輸入 #案件PDF 產生 PDF'}
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
                save_case(cid,fields,user,warnings)
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
        if typ=='text' and (raw.startswith(('#補充案件','補充案件')) or (not raw.startswith('#') and any(w in raw for w in ('地址','租金','開價','售價','格局','管理費','房','屋齡')))):
            fields=parse_text(raw.replace('#補充案件','',1).replace('補充案件','',1))
            if not fields: return {'handled':True,'ok':False,'reply_text':'未找到可辨識欄位，請使用「開價1588萬，3房2廳2衛」或「地址：...」等格式。'}
            save_case(cid,fields,user)
            return {'handled':True,'ok':True,'reply_text':'✅ 已更新\n'+'\n'.join(f'{FIELDS[k]}：{v}' for k,v in fields.items())}
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
