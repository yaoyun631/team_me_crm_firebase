# -*- coding: utf-8 -*-
"""PRIM/愛屋/樂屋物件庫兼容層：主程式最末段呼叫 install(app_module_globals)。"""
import re, hashlib

ALIASES={
 'case':['物件編號','案件編號','案號','物件代碼','編號','案編','raw_物件編號','rakuya_物件編號'],
 'branch':['分店代碼','店代碼','分店','店別','raw_分店代碼'],
 'title':['rakuya_物件名稱','raw_標題','物件名稱','案件名稱','物件標題','案名','標題','名稱','name','title'],
 'address':['rakuya_地址全文','raw_完整地址','完整地址','物件地址','地址','地址全文','address'],
 'city':['rakuya_縣市','縣市','所在縣市','城市','city'],
 'area':['rakuya_行政區','行政區','鄉鎮市區','所在區域','區域','area'],
 'type':['rakuya_現況類型','raw_類型現況','產品類型','物件類型','案件類型','類型','建物型態','物件型態','用途','property_type'],
 'community':['rakuya_社區名稱','raw_社區','社區名稱','社區','建案名稱','建案'],
 'url':['rakuya_刊登來源網址','raw_型錄網址','物件連結','物件網址','型錄網址','網址','連結','url','source_url'],
 'description':['rakuya_特色描述','raw_環境特色','特色描述','物件特色','特色','賣點','廣告文案','備註','說明','description'],
 'price':['rakuya_總價_萬','總價_萬','raw_委託總價','委託總價','售價','開價','銷售價格','總價','價格','price_wan'],
 'rent':['rakuya_租金_元','租金_元','月租金','出租價格','租金','月租','rent_price'],
 'rooms':['rakuya_房','房間數','房數','房','rooms'],
 'halls':['rakuya_廳','廳數','廳','halls'],
 'baths':['rakuya_衛','衛浴數','衛數','衛','baths'],
 'age':['rakuya_屋齡','raw_屋齡','屋齡','age'],
 'building':['rakuya_建物登記','rakuya_權狀面積','raw_建物面積','raw_登記坪數','建物面積','建坪','權狀坪數','權狀面積','總建坪','登記坪數','建物登記','building_area'],
 'main':['rakuya_主建物','raw_主建物坪','主建物坪數','主建物','主建坪','主建物坪','室內坪','使用坪數','main_building_area'],
 'land':['rakuya_土地登記','raw_土地登記','土地總坪數','土地登記坪數','土地坪數','土地登記','地坪','土地坪','land_area'],
 'floor':['rakuya_出售樓層','所在樓層','樓層','樓別','floor'],
 'total_floor':['rakuya_總樓層','建物總樓層','總樓層','總樓高','floor_total'],
 'parking':['rakuya_車位','車位類型','車位方式','停車位','車位','parking'],
 'deal':['交易類別','交易型態','交易類型','委託類別','案件分類','租售類別','租售','出售出租','deal_type'],
 'images':['照片網址','圖片網址','照片連結','圖片連結','相片網址','照片URL','圖片URL','images','image_urls'],
}
def norm(s):return re.sub(r'[\s_\-:：\(\)（）\[\]【】/\\]+','',str(s or '')).lower()
def get(row, key):
    keys={norm(k):v for k,v in row.items()}
    for alias in ALIASES[key]:
        v=keys.get(norm(alias))
        if v is not None and str(v).strip() and str(v).lower()!='nan':return str(v).strip()
    return ''
def number(v):
    m=re.search(r'-?\d[\d,]*(?:\.\d+)?',str(v or ''))
    return float(m.group(0).replace(',','')) if m else None
def install(g):
    old_detect=g['_property_csv_detect_deal_type'];old_row=g['_property_csv_row_to_property'];old_id=g['_property_csv_doc_id']
    def deal(filename,headers=None):
        name=str(filename or '').lower()
        if any(s in name for s in ('出租','_租','rent')):return 'rent'
        if any(s in name for s in ('出售','_售','sale')):return 'sale'
        # PRIM 標題通常同時存在租售欄，僅由欄名猜測會誤判。
        h=[norm(s) for s in (headers or [])]
        if '租金' in h and not any(x in h for x in ('總價','售價','委託總價')):return 'rent'
        return old_detect(filename,headers)
    def row_to_property(row,deal_type,filename):
        data=old_row(row,deal_type,filename)
        if 'prim' not in filename.lower() and not any('prim' in str(k).lower() for k in row):return data
        mapping={'title':'title','address':'address','city':'city','area':'area','property_type':'type','community':'community','url':'url','description':'description','floor':'floor','total_floor':'total_floor','parking':'parking'}
        for dest,key in mapping.items():
            value=get(row,key)
            if value:data[dest]=value
        for dest,key in [('rooms','rooms'),('halls','halls'),('baths','baths'),('age','age'),('building_area','building'),('main_building_area','main'),('land_area','land')]:
            val=number(get(row,key))
            if val is not None:data[dest]=val
        value=get(row,'price' if deal_type=='sale' else 'rent')
        val=number(value)
        if val is not None:
            if deal_type=='sale':
                # PRIM 售價慣例為「萬」；若資料是元且大於10萬，轉為萬元。
                data['price_wan']=val/10000 if val>=100000 else val
            else:
                # 例如 1.8萬 -> 18000元；18000 -> 不再乘萬。
                data['rent_price']=val*10000 if '萬' in value else val
        data['source_case_no']=get(row,'case') or data.get('source_case_no','')
        data['source_branch']=get(row,'branch') or data.get('source_branch','')
        data['source_system']='PRIM'
        search=[data.get(k) for k in ['title','address','city','area','property_type','community','description','parking','source_case_no']]
        search.extend(str(x) for x in [data.get('rooms'),data.get('building_area'),data.get('land_area')] if x is not None)
        data['searchable_text']=' '.join(str(v) for v in search if v)
        data['photo_urls_raw']=get(row,'images')
        return data
    def doc_id(row,deal_type):
        case=get(row,'case');branch=get(row,'branch')
        if case:
            key='|'.join([deal_type,branch,case])
            return deal_type+'_'+hashlib.sha1(key.encode('utf-8')).hexdigest()[:20]
        return old_id(row,deal_type)
    g['_property_csv_detect_deal_type']=deal
    g['_property_csv_row_to_property']=row_to_property
    g['_property_csv_doc_id']=doc_id
