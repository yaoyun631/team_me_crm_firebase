# -*- coding: utf-8 -*-
"""Team M.E PRIM v2.8 adapter. Compatible with the earlier 'install(globals())' interface."""
import re, hashlib, json

def norm(s):
    return str(s or '').strip().replace('臺','台')

def get(row, *names):
    for name in names:
        x=row.get(name)
        if x is not None and str(x).strip() and str(x).strip().lower() not in ('nan','none','null'):
            return str(x).strip()
    return ''

def nfloat(value):
    m=re.search(r'-?\d[\d,]*(?:\.\d+)?',str(value or ''))
    return float(m.group(0).replace(',','')) if m else None

def wan(value, already_wan=False):
    v=nfloat(value)
    if v is None:return None
    return v if already_wan or '萬' in str(value) else v / 10000

def is_prim(row):
    # Don't identify an old 愛屋 CSV as PRIM solely because the filename contains PRIM.
    return bool(('obj_no' in row and 'obj_name' in row) or ('obj_uuid' in row and 'sell_rent' in row))

def clean_address(value):
    s=norm(value)
    # Handle generated "台中市梧棲區臺中市梧棲區..." without stripping legitimate street names.
    m=re.match(r'^(?P<prefix>[^市縣]{1,8}[市縣][^區鄉鎮市]{1,9}[區鄉鎮市])(?P=prefix)',s)
    if m:s=s[len(m.group('prefix')):]
    return s

def photo_urls(row):
    urls=[]
    def add(x):
        x=str(x or '').strip()
        if x.startswith('https://') and x not in urls:urls.append(x)
    # Prefer explicit cover, then gallery, avoid internal photo manifest with massive JSON.
    add(get(row,'cover_photo','cover_photo_original'))
    for i in range(1,39):add(row.get(f'照片{i}'))
    return urls

def property_from_prim(row,deal_type):
    title=get(row,'obj_name','rakuya_物件名稱','raw_標題')
    address=clean_address(get(row,'address','rakuya_地址全文','raw_完整地址'))
    city=norm(get(row,'city','rakuya_縣市'))
    area=norm(get(row,'area','rakuya_行政區'))
    price=wan(get(row,'price')) if get(row,'price') else wan(get(row,'rakuya_總價_萬'),True)
    # PRIM rent prices may be in yuan; do not interpret a sale price as rent.
    rent=nfloat(get(row,'rent','rent_price','rakuya_租金_元'))
    if deal_type=='rent' and rent is not None and '萬' in get(row,'rent','rent_price','rakuya_租金_元'):rent*=10000
    build=nfloat(get(row,'build_ping','ping','rakuya_建物登記'))
    land=nfloat(get(row,'land_ping','rakuya_土地登記'))
    imgs=photo_urls(row)
    case_no=get(row,'obj_no','prim_物件編號','raw_物件編號')
    legacy_no=get(row,'contract_no','raw_愛屋物件編號')
    prop_type=get(row,'raw_類型現況','rakuya_現況類型','obj_class')
    desc=get(row,'feature','rakuya_特色描述','raw_環境特色')
    parking=get(row,'parking_type','rakuya_車位')
    # Non-sensitive, bounded metadata only; PRIM source also contains agents' phone numbers
    # and potentially large photo manifests. Don't duplicate 393 columns in Firestore raw.
    searchable=' '.join(str(x) for x in [title,city,area,address,prop_type,get(row,'community'),desc,parking,case_no,legacy_no,get(row,'road_name'),get(row,'contract_no')] if x)
    return {
        'active':True,'deal_type':deal_type,'title':title or '未命名物件',
        'address':address,'city':city,'area':area,'property_type':prop_type,
        'community':get(row,'community','rakuya_社區名稱'), 'description':desc,
        'price_wan':price if deal_type=='sale' else None,
        'rent_price':rent if deal_type=='rent' else None,
        'building_area':build,'main_building_area':nfloat(get(row,'major_space','rakuya_主建物')),
        'land_area':land,'rooms':nfloat(get(row,'room','rakuya_房')),
        'halls':nfloat(get(row,'living','rakuya_廳')),
        'baths':nfloat(get(row,'bathroom','rakuya_衛')),
        'age':nfloat(get(row,'rakuya_屋齡','raw_屋齡')),
        'floor':get(row,'floor','rakuya_出售樓層'),
        'total_floor':get(row,'up_floors','rakuya_總樓層'),
        'parking':parking,
        'url':get(row,'rakuya_post_url','rakuya_刊登來源網址'),
        'source_system':'PRIM','source_case_no':case_no,
        'source_legacy_case_no':legacy_no,'source_branch':'PRIM',
        'searchable_text':searchable,
        'cover_photo':imgs[0] if imgs else '',
        'photo_urls':imgs,'photo_urls_raw':'\n'.join(imgs),
        'photo_count':len(imgs),
        'raw':{'raw_物件編號':case_no,'raw_愛屋物件編號':legacy_no,
               'raw_標題':title,'raw_完整地址':address,
               'raw_委託總價':str(price) if price is not None else '',
               'raw_登記坪數':str(build) if build is not None else '',
               'raw_土地登記':str(land) if land is not None else '',
               'raw_類型現況':prop_type,'raw_社區':get(row,'community'),
               'raw_環境特色':desc},
    }

def install(g):
    old_detect=g['_property_csv_detect_deal_type']
    old_convert=g['_property_csv_row_to_property']
    old_id=g['_property_csv_doc_id']
    def detect(filename,headers=None):
        fn=str(filename or '').lower()
        if '出租' in fn or 'rent' in fn:return 'rent'
        if '出售' in fn or 'sell' in fn or '樂屋工作檔' in fn:return 'sale'
        return old_detect(filename,headers)
    def convert(row,deal_type,filename):
        return property_from_prim(row,deal_type) if is_prim(row) else old_convert(row,deal_type,filename)
    def doc_id(row,deal_type):
        if is_prim(row):
            case=get(row,'obj_no','prim_物件編號','raw_物件編號')
            if case:
                return deal_type+'_'+hashlib.sha1(('prim|'+deal_type+'|'+case).encode()).hexdigest()[:20]
        return old_id(row,deal_type)
    g['_property_csv_detect_deal_type']=detect
    g['_property_csv_row_to_property']=convert
    g['_property_csv_doc_id']=doc_id
