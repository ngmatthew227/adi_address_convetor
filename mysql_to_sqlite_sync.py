import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import pandas as pd
from sqlalchemy import create_engine, text

try:
    import zhconv
except ImportError as exc:  # pragma: no cover
    raise ImportError(
        "需要 zhconv 做繁簡轉換，請先執行: pip install zhconv"
    ) from exc

try:
    from pyproj import Transformer
except ImportError:  # pragma: no cover
    Transformer = None

# ==========================================
# MySQL to SQLite 自動轉移腳本 (乾淨修正版)
# ==========================================
# ⚠️ 請將以下連線資訊替換為您的實際設定
MYSQL_USER = 'root'
MYSQL_PASS = 'xYzBbIjku!56'
MYSQL_HOST = '172.18.0.22'
MYSQL_DB   = 'EADI100'
mysql_engine = create_engine(f'mysql+pymysql://{MYSQL_USER}:{MYSQL_PASS}@{MYSQL_HOST}:3306/{MYSQL_DB}')
sqlite_engine = create_engine('sqlite:///adi_address.sqlite')

SCRIPT_DIR = Path(__file__).resolve().parent
SUB_DISTRICT_MAP_DDL = SCRIPT_DIR / 'sub_district_map.sql'
SUB_DISTRICT_MAP_DATA = SCRIPT_DIR / 'sub_district_map_data.sql'
IDENTIFY_RESULT_JSON = SCRIPT_DIR / 'missing_street_identify.json'
STREET_NAMES_JSON = SCRIPT_DIR / 'street_names.json'
IDENTIFY_API_URL = 'https://www.map.gov.hk/gs/api/v1.0.0/identify'
GEODETIC_TRANSFORM_URL = 'https://www.geodetic.gov.hk/transform/v2/'
IDENTIFY_REQUEST_INTERVAL_SEC = 0.35

# 已經完全清除隱藏字元與非法空格的 SQL 語句
sql_query = """
SELECT
    t.ADDRESS2DID,
    t.REFCSUID,
    t.REGION,
    t.District_Name_Chi,
    t.District_Name_Eng,
    t.Street_Full_Name_Chi,
    t.Street_Full_Name_Eng,
    t.Street_Type_Chi,
    t.Street_Type_Eng,
    t.Building_No,
    t.Estate_Name_Chi,
    t.Estate_Name_Eng,
    t.Phase_Name_Chi,
    t.Phase_Name_Eng,
    t.Building_Name_Chi,
    t.Building_Name_Eng,
    IF(t.LONGITUDE IS NOT NULL AND t.LATITUDE IS NOT NULL,
       CONCAT(TRIM(t.LONGITUDE), ',', TRIM(t.LATITUDE)), NULL) AS Coordinates
FROM (
    SELECT
        a.ADDRESS2DID, a.REFCSUID, d.REGION,
        d.NAMECHI AS District_Name_Chi, d.NAMEENG AS District_Name_Eng,
        COALESCE(sn.CHINAME, snl.CHIFULLNAME) AS Street_Full_Name_Chi,
        COALESCE(sn.ENGNAME, snl.ENGFULLNAME) AS Street_Full_Name_Eng,
        sn.CHITYPE AS Street_Type_Chi, sn.ENGTYPE AS Street_Type_Eng,
        NULLIF(CONCAT(
            IFNULL(a.BUILDINGNUMFROM, ''),
            IFNULL(a.BUILDINGNUMFROMALPHA, ''),
            -- EXT 拼接規則：
            -- 1) 若已等於 FROMALPHA 末尾字元，不拼（如 A1 + 1 → 不拼 1）
            -- 2) 若等於 TO_A，不拼到起號（如 8 + EXT10 + TO 10 → 8-10，而非 810-10）
            IF(
                a.BUILDINGNUMEXT IS NOT NULL
                AND a.BUILDINGNUMEXT != ''
                AND (
                    a.BUILDINGNUMFROMALPHA IS NULL
                    OR RIGHT(a.BUILDINGNUMFROMALPHA, CHAR_LENGTH(a.BUILDINGNUMEXT))
                       != a.BUILDINGNUMEXT
                )
                AND (
                    a.BUILDINGNUMTO_A IS NULL
                    OR CAST(a.BUILDINGNUMTO_A AS CHAR) != a.BUILDINGNUMEXT
                ),
                a.BUILDINGNUMEXT,
                ''
            ),
            IF(a.BUILDINGNUMTO_A IS NOT NULL,
               CONCAT('-', a.BUILDINGNUMTO_A, IFNULL(a.BUILDINGNUMTOALPHA_A, '')), '')
        ), '') AS Building_No,
        dn.CHIESTATENAME AS Estate_Name_Chi, dn.ENGESTATENAME AS Estate_Name_Eng,
        dn.CHIPHASENAME AS Phase_Name_Chi, dn.ENGPHASENAME AS Phase_Name_Eng,
        bn.CHIBUILDINGNAME AS Building_Name_Chi, bn.ENGBUILDINGNAME AS Building_Name_Eng,
        g.LONGITUDE, g.LATITUDE
    FROM ADDRESS2D a
    LEFT JOIN DISTRICT d ON a.DISTRICTCODE = d.DISTRICTCODE
    LEFT JOIN STREETNAMELOCATION snl ON a.STREETLOCID = snl.STREETLOCID
    LEFT JOIN STREETNAME sn ON snl.STREETID = sn.STREETID
    LEFT JOIN DEVELOPMENTNAME dn ON a.DEVELOPMENTID = dn.DEVELOPMENTID
    LEFT JOIN BUILDINGNAME bn ON a.REFCSUID = bn.BUILDINGCSUID
    LEFT JOIN GEOINFO g ON a.REFCSUID = g.BUILDINGCSUID
) t;
"""

TC_REGION = {'NT': '新界', 'KLN': '九龍', 'HK': '香港'}
SC_REGION = {'NT': '新界', 'KLN': '九龙', 'HK': '香港'}
EN_REGION = {'NT': 'NEW TERRITORIES', 'KLN': 'KOWLOON', 'HK': 'HONG KONG'}
OUTPUT_COLUMNS = [
    'id',
    'ref_csuid',
    'tc_region',
    'tc_district',
    'tc_street_name',
    'tc_street_no',
    'tc_full_addr',
    'tc_building_field_label',
    'tc_building_field_value',
    'sc_region',
    'sc_district',
    'sc_street_name',
    'sc_street_no',
    'sc_full_addr',
    'sc_building_field_label',
    'sc_building_field_value',
    'en_region',
    'en_district',
    'en_street_name',
    'en_street_no',
    'en_full_addr',
    'en_building_field_label',
    'en_building_field_value',
    'coordinates',
]

_CJK_RE = re.compile(r'([\u4e00-\u9fff])')
_FTS_TEXT_COLS = (
    'tc_district',
    'tc_street_name',
    'tc_full_addr',
    'tc_building_field_label',
    'tc_building_field_value',
    'sc_district',
    'sc_street_name',
    'sc_full_addr',
    'sc_building_field_label',
    'sc_building_field_value',
    'en_district',
    'en_street_name',
    'en_full_addr',
    'en_building_field_label',
    'en_building_field_value',
)


_STREET_CHI_PATCH = {
    'CHUT SHUI WAN': '出水灣',
    'MUK WO STREET': '沐和街',
    'OLYMPIC AVENUE': '世運道',
}


def _clean(value):
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    text = str(value).strip()
    return text or None


_BUILDING_NO_RANGE_RE = re.compile(
    r'^([0-9][0-9A-Za-z]*)-([0-9][0-9A-Za-z]*)$'
)
_BUILDING_NO_SLASH_RANGE_RE = re.compile(
    r'^([0-9]+[A-Za-z]*)/([0-9A-Za-z]+)-([0-9]+[A-Za-z]*)$'
)
_BUILDING_DIGITS_RE = re.compile(r'^([0-9]+)')


def _normalize_building_no(value):
    """修正門牌起訖被錯誤串成「{from}{to}-{to}」的情況。

    例:
      1819-19     → 18-19
      114117-117  → 114-117
      125A125-125 → 125A-125
      8898-98     → 88-98

    判斷: 左段以右段結尾，去掉右段後仍像門牌，且數字位數合理
    （右段位數不大於左段），避免誤傷如 120-20。
    """
    text = _clean(value)
    if text is None:
        return None

    # OFF 在來源資料多為 "off <street>" 的縮寫標記，非有效門牌，轉為空值。
    if text.upper() == 'OFF':
        return None

    # 「*」為來源佔位門牌（會變成 *號），非有效地址，由上游直接丟棄整列。
    if text == '*':
        return None

    # 修正 a/b-b 與 a/b-c 類型（含字母）
    # 例:
    #   1/13-13     -> 1-13
    #   1/1A-1A     -> 1-1A
    #   103F/103H-103H -> 103F-103H
    #   123A/B-123B -> 123A-123B
    slash_m = _BUILDING_NO_SLASH_RANGE_RE.match(text)
    if slash_m:
        left, mid, right = slash_m.group(1), slash_m.group(2), slash_m.group(3)
        if mid == right:
            return f'{left}-{right}'
        # 允許中段僅提供右段尾碼（如 B -> 123B）
        if right.endswith(mid):
            left_digits = _BUILDING_DIGITS_RE.match(left)
            right_digits = _BUILDING_DIGITS_RE.match(right)
            if left_digits and right_digits and left_digits.group(1) == right_digits.group(1):
                return f'{left}-{right}'

    m = _BUILDING_NO_RANGE_RE.match(text)
    if not m:
        return text

    left, right = m.group(1), m.group(2)
    if left == right or not left.endswith(right):
        return text

    cand = left[: -len(right)]
    if not cand or not re.match(r'^[0-9]+[A-Za-z]*$', cand):
        return text

    left_digits = _BUILDING_DIGITS_RE.match(cand)
    right_digits = _BUILDING_DIGITS_RE.match(right)
    if not left_digits or not right_digits:
        return text
    # 起號數字位數應 ≥ 訖號（串錯時長度相近；120-20 → cand=1 會被擋）
    if len(left_digits.group(1)) < len(right_digits.group(1)):
        return text

    return f'{cand}-{right}'


# 單一門牌段：1–5 位數字（不可前導 0）+ 最多 1 個英文字母
_STREET_NO_PART_RE = re.compile(r'^([1-9][0-9]{0,4})([A-Za-z]?)$')


def _is_normal_street_no(value) -> bool:
    """判斷門牌是否「正常」。

    正常例子: 12 / 12A / 12-14 / 7A-7C
    異常例子: 012 / 12AB / 181919 / 7C-7A / 1/13 / LOT / 12-12
    """
    text = _clean(value)
    if text is None:
        return True  # 空值不算異常門牌格式

    # 單段或起訖兩段
    parts = text.split('-')
    if len(parts) not in (1, 2):
        return False

    parsed = []
    for part in parts:
        m = _STREET_NO_PART_RE.match(part)
        if not m:
            return False
        num = int(m.group(1))
        letter = m.group(2).upper()
        parsed.append((num, letter))

    if len(parsed) == 1:
        return True

    left_num, left_letter = parsed[0]
    right_num, right_letter = parsed[1]

    # 起訖相同（整段相等）不合理，如 12-12 / 12A-12A
    if left_num == right_num and left_letter == right_letter:
        return False

    # 起號不得大於訖號；同號時字母必須遞增（A < C）
    if left_num > right_num:
        return False
    if left_num == right_num and left_letter >= right_letter:
        return False

    return True


def print_strange_street_nos(engine):
    """列出正規化後仍不像一般門牌的 tc_street_no（含筆數）。"""
    print("\n🔎 異常 tc_street_no 清單（嚴格規則）：")
    print("   正常: 1–5位數字(無前導0) + 最多1字母；起訖需遞增")
    print("   例正常: 12 / 12A / 12-14 / 7A-7C")
    with engine.connect() as conn:
        rows = conn.execute(text("""
            SELECT tc_street_no, COUNT(*) AS cnt
            FROM Address_Flattened
            WHERE tc_street_no IS NOT NULL
              AND tc_street_no != ''
            GROUP BY tc_street_no
            ORDER BY cnt DESC, tc_street_no
        """)).fetchall()

    strange = [
        (no, cnt) for no, cnt in rows
        if not _is_normal_street_no(no)
    ]
    if not strange:
        print("   (沒有異常 street_no)")
        return

    print(f"   共 {len(strange)} 種異常值（總筆數 {sum(c for _, c in strange)}）:")
    for no, cnt in strange:
        print(f"   - {no!r}  ({cnt})")


def _to_sc(value):
    text = _clean(value)
    if text is None:
        return None
    return zhconv.convert(text, 'zh-cn')


# 依 STREETNAME.CHITYPE / ENGTYPE 實際枚舉：鄉村／圍村類型白名單
_HK_VILLAGE_TYPES_CHI = frozenset({
    '村', '圍', '新村', '下村', '舊村', '上村', '中村', '北村',
    '東村', '西村', '南村', '後村', '舊圍', '新圍', '遷建村',
})
_HK_VILLAGE_TYPES_ENG = frozenset({
    'wai', 'tsuen', 'village', 'new village', 'ha tsuen',
    'resite village', 'resited village', 'kau tsuen', 'san tsuen',
    'sheung tsuen', 'chung tsuen', 'north tsuen', 'north village',
    'upper village', 'kau wai', 'east village', 'east tsuen',
    'san wai', 'back tsuen', 'south tsuen', 'west tsuen',
    'west village',
})

# 類型為 NULL 時，用全名結尾作後備（避免「廈村路」——有類型「路」會在上面被排除）
_HK_VILLAGE_SUFFIX_CHI = tuple(_HK_VILLAGE_TYPES_CHI)
_HK_VILLAGE_SUFFIX_ENG = re.compile(
    r'(?:^|[\s\-])(?:'
    r'walled\s+village|resite[d]?\s+village|upper\s+village|'
    r'new\s+village|east\s+village|west\s+village|north\s+village|'
    r'ha\s+tsuen|kau\s+tsuen|san\s+tsuen|sheung\s+tsuen|chung\s+tsuen|'
    r'north\s+tsuen|east\s+tsuen|south\s+tsuen|west\s+tsuen|back\s+tsuen|'
    r'kau\s+wai|san\s+wai|village|tsuen|wai'
    r')$',
    re.IGNORECASE,
)


def _is_village(row) -> bool:
    """以香港 STREETNAME 類型枚舉判斷是否鄉村／圍村門牌。

    規則:
    1. CHITYPE / ENGTYPE 落在鄉村白名單 → 是
       （含：村、圍、新村、遷建村、TSUEN、VILLAGE、WAI…）
    2. 已有非空類型但不在白名單 → 否
       （含：街、路、道、邨、ESTATE、RD、ST…，避免「廈村路」）
    3. 類型為 NULL 時，才用中英文全名結尾作後備
    """
    type_chi = _clean(row.get('Street_Type_Chi')) or ''
    type_eng = (_clean(row.get('Street_Type_Eng')) or '').casefold().strip()
    street_chi = _clean(row.get('Street_Full_Name_Chi')) or ''
    street_eng = _clean(row.get('Street_Full_Name_Eng')) or ''

    # 1) 官方類型白名單
    if type_chi in _HK_VILLAGE_TYPES_CHI:
        return True
    if type_eng in _HK_VILLAGE_TYPES_ENG:
        return True

    # 2) 有明確非鄉村類型 → 非鄉村
    if type_chi or type_eng:
        return False

    # 3) 類型 NULL：名稱結尾後備
    if street_chi.endswith(_HK_VILLAGE_SUFFIX_CHI):
        return True
    if street_eng and _HK_VILLAGE_SUFFIX_ENG.search(street_eng.strip()):
        return True

    return False


def _street_display_name_chi(row) -> str | None:
    """中文街名顯示：街名 + 類型（如 坪洋新+村、彌敦+道）；已含類型結尾則不重複拼。"""
    street = _clean(row.get('Street_Full_Name_Chi'))
    if street:
        street = _STREET_CHI_PATCH.get(street.upper(), street)
    type_chi = _clean(row.get('Street_Type_Chi'))
    if street and type_chi:
        if street.endswith(type_chi):
            return street
        return f'{street}{type_chi}'
    return street or type_chi


def _street_display_name_eng(row) -> str | None:
    """英文街名顯示：街名 + 類型（如 PING YEUNG NEW + VILLAGE）；已含類型結尾則不重複拼。"""
    street = _clean(row.get('Street_Full_Name_Eng'))
    type_eng = _clean(row.get('Street_Type_Eng'))
    if street and type_eng:
        if street.casefold().endswith(type_eng.casefold()):
            return street
        return f'{street} {type_eng}'
    return street or type_eng


def _build_tc_parts(row):
    district = _clean(row.get('District_Name_Chi'))
    street_no = _normalize_building_no(row.get('Building_No'))
    estate = _clean(row.get('Estate_Name_Chi'))
    phase = _clean(row.get('Phase_Name_Chi'))
    building = _clean(row.get('Building_Name_Chi'))
    is_village = _is_village(row)

    # 一律拼上類型（村／道／街…）；鄉村不寫入 street 欄
    display_street = _street_display_name_chi(row)
    if is_village:
        out_street = None
        out_street_no = None
    else:
        out_street = display_street
        out_street_no = street_no

    no_with_unit = f'{street_no}號' if street_no else None

    # estate/phase/building 片段
    estate_part = None
    if estate or phase or building:
        estate_part = estate or ''
        if phase:
            estate_part = f'{estate_part} {phase}' if estate_part else phase
        if building:
            estate_part = f'{estate_part} {building}' if estate_part else building

    # label: 不含 region / district，格式為 <street><no號> <estate/phase/building>
    if display_street and no_with_unit:
        street_and_no = f'{display_street}{no_with_unit}'
    else:
        street_and_no = display_street or no_with_unit
    label = ' '.join(p for p in [street_and_no, estate_part] if p) or None

    # value:
    # - 一般: 不含 region / district / street / street_no (= estate_part)
    # - 鄉村: 不含 region / district (= label，已含村名+類型)
    if is_village:
        value = label
    else:
        value = estate_part

    # full: district + label（舊格式不含 region 字面）
    full = ''.join(p for p in [district, label] if p) or None
    return district, out_street, out_street_no, full, label, value


def _build_en_parts(row, en_region, en_district):
    street_no = _normalize_building_no(row.get('Building_No'))
    estate = _clean(row.get('Estate_Name_Eng'))
    phase = _clean(row.get('Phase_Name_Eng'))
    building = _clean(row.get('Building_Name_Eng'))
    is_village = _is_village(row)

    # 一律拼上類型（VILLAGE / ROAD…）；鄉村不寫入 street 欄
    display_street = _street_display_name_eng(row)
    if is_village:
        out_street = None
        out_street_no = None
    else:
        out_street = display_street
        out_street_no = street_no

    estate_part = ', '.join(p for p in [building, phase, estate] if p) or None

    if street_no and display_street:
        street_part = f'{street_no} {display_street}'
    else:
        street_part = street_no or display_street

    # label: 不含 region / district
    label = ', '.join(p for p in [building, phase, estate, street_part] if p) or None

    # value: 鄉村保留 village/no；一般則不含
    if is_village:
        value = label
    else:
        value = estate_part

    # full: label + district + region
    full = ', '.join(p for p in [label, en_district, en_region] if p) or None
    return out_street, out_street_no, full, label, value


def _is_placeholder_building_no(value) -> bool:
    """來源門牌為佔位符（如 *）→ 整列地址無效。"""
    text = _clean(value)
    return text == '*'


def _is_district_only_addr(tc_district, tc_full, en_district, en_full, en_region) -> bool:
    """只有行政區、無街名／樓宇等細節 → 無效地址。

    中文: tc_district == tc_full_addr（如「北區」）
    英文: en_full 僅為 district，或 district + region
          （如「NORTH DISTRICT, NEW TERRITORIES」）
    """
    if not (tc_district and tc_full and tc_district == tc_full):
        return False
    if not en_district or not en_full:
        return True
    if en_full == en_district:
        return True
    if en_region and en_full == f'{en_district}, {en_region}':
        return True
    return False


def _parse_lon_lat(coordinates):
    """解析 'lon,lat' 字串 → (lon, lat)；失敗回 None。"""
    text = _clean(coordinates)
    if not text or ',' not in text:
        return None
    left, right = text.split(',', 1)
    try:
        lon = float(left.strip())
        lat = float(right.strip())
    except ValueError:
        return None
    return lon, lat


def _http_get_json(url: str, timeout: float = 30):
    req = urllib.request.Request(
        url,
        headers={
            'User-Agent': 'adi-address-convertor/1.0',
            'Accept': 'application/json',
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode('utf-8')
    return json.loads(raw)


_HK80_TRANSFORMER = None


def _get_hk80_transformer():
    global _HK80_TRANSFORMER
    if Transformer is None:
        return None
    if _HK80_TRANSFORMER is None:
        _HK80_TRANSFORMER = Transformer.from_crs(
            'EPSG:4326', 'EPSG:2326', always_xy=True,
        )
    return _HK80_TRANSFORMER


def wgs84_to_hk80(lon: float, lat: float):
    """WGS84 lon/lat → HK1980 Grid (easting, northing)。

    優先 pyproj；否則改打 LandsD transform API。
    """
    transformer = _get_hk80_transformer()
    if transformer is not None:
        easting, northing = transformer.transform(lon, lat)
        return float(easting), float(northing)

    qs = urllib.parse.urlencode({
        'inSys': 'wgsgeog',
        'outSys': 'hkgrid',
        'lat': f'{lat:.8f}',
        'long': f'{lon:.8f}',
    })
    data = _http_get_json(f'{GEODETIC_TRANSFORM_URL}?{qs}')
    return float(data['hkE']), float(data['hkN'])


def call_identify_api(easting: float, northing: float, lang: str = 'zh'):
    """呼叫 CSDI Identify API（HK80 easting/northing）。

    文件: https://portal.csdi.gov.hk/csdi-webpage/apidoc/IdentifyAPI
    """
    qs = urllib.parse.urlencode({
        'x': f'{easting:.3f}',
        'y': f'{northing:.3f}',
        'lang': lang,
    })
    payload = _http_get_json(f'{IDENTIFY_API_URL}?{qs}')
    blocks = payload.get('results') or []
    building_only = [
        block for block in blocks
        if (block.get('eheader') or '').strip() == 'Building Information'
    ]
    payload['results'] = building_only
    return payload


def fetch_identify_for_missing_streets(targets, output_path=None, lang='zh'):
    """對「有 street_no、無 street_name」地址呼叫 Identify，寫入 JSON。"""
    output_path = Path(output_path or IDENTIFY_RESULT_JSON)
    results = []
    total = len(targets)
    if total == 0:
        output_path.write_text('[]', encoding='utf-8')
        print('ℹ️ 沒有「有門牌、無街名」的地址需要呼叫 Identify API。')
        return results

    print(f'🛰️ 開始呼叫 Identify API：共 {total} 筆（間隔 {IDENTIFY_REQUEST_INTERVAL_SEC}s）...')
    for i, target in enumerate(targets, start=1):
        entry = {
            **target,
            'hk80': None,
            'identify': None,
            'error': None,
        }
        coords = _parse_lon_lat(target.get('coordinates'))
        if coords is None:
            entry['error'] = 'missing_or_invalid_coordinates'
            results.append(entry)
            continue
        lon, lat = coords
        try:
            easting, northing = wgs84_to_hk80(lon, lat)
            entry['hk80'] = {'x': easting, 'y': northing}
            entry['identify'] = call_identify_api(easting, northing, lang=lang)
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, KeyError, ValueError, json.JSONDecodeError) as exc:
            entry['error'] = str(exc)
        results.append(entry)
        if i % 20 == 0 or i == total:
            print(f'   … Identify 進度 {i}/{total}')
        if i < total:
            time.sleep(IDENTIFY_REQUEST_INTERVAL_SEC)

    output_path.write_text(
        json.dumps(results, ensure_ascii=False, indent=2),
        encoding='utf-8',
    )
    ok_n = sum(1 for r in results if r.get('identify') is not None)
    err_n = sum(1 for r in results if r.get('error'))
    print(f'✅ Identify 結果已寫入 {output_path}（成功 {ok_n}，失敗/缺座標 {err_n}）')
    return results


def _first_identify_address_info(entry):
    identify = entry.get('identify') or {}
    results = identify.get('results') or []
    if not results:
        return None, None
    first_result = results[0]
    infos = first_result.get('addressInfo') or []
    if not infos:
        return first_result, None
    return first_result, infos[0]


def _normalize_street_key(value: str | None) -> str | None:
    """街名比對用：去空白、統一橫線、英文轉大寫。"""
    text = _clean(value)
    if text is None:
        return None
    text = re.sub(r'[\s\u3000]+', '', text)
    text = text.replace('－', '-').replace('‐', '-').replace('–', '-').replace('—', '-')
    return text.casefold()


_STREET_NAME_INDEX = None


def _load_street_name_index(path=None):
    """載入 street_names.json → {norm_chi/norm_eng: (chi, eng)}。"""
    global _STREET_NAME_INDEX
    if _STREET_NAME_INDEX is not None:
        return _STREET_NAME_INDEX

    json_path = Path(path or STREET_NAMES_JSON)
    index = {}
    if not json_path.is_file():
        print(f'⚠️ 找不到街名表 {json_path}，Identify 回填將無法核對街名。')
        _STREET_NAME_INDEX = index
        return index

    rows = json.loads(json_path.read_text(encoding='utf-8'))
    for row in rows:
        chi = _clean(row.get('chi_street_name'))
        eng = _clean(row.get('eng_street_name'))
        if chi:
            index[_normalize_street_key(chi)] = (chi, eng)
        if eng:
            index[_normalize_street_key(eng)] = (chi, eng)
    _STREET_NAME_INDEX = index
    print(f'📘 已載入街名表 {len(rows)} 筆（索引 {len(index)} 個 key）。')
    return index


def _strip_chi_street_no(caddress: str | None, street_no: str | None) -> str | None:
    """從中文地址去掉門牌號，例: 沙頭角公路－龍躍頭段192號 → 沙頭角公路－龍躍頭段。"""
    text = _clean(caddress)
    if text is None:
        return None
    no = _clean(street_no)
    if no:
        for suffix in (f'{no}號', no):
            if text.endswith(suffix):
                return _clean(text[: -len(suffix)])
    m = re.search(r'(.+?)([0-9][0-9A-Za-z]*(?:-[0-9][0-9A-Za-z]*)?)號$', text)
    if m:
        return _clean(m.group(1))
    return text


def _strip_en_street_no(eaddress: str | None, street_no: str | None) -> str | None:
    """從英文地址去掉門牌號，例: 192 SHA TAU KOK ROAD - LUNG YEUK TAU → SHA TAU..."""
    text = _clean(eaddress)
    if text is None:
        return None
    no = _clean(street_no)
    if no:
        prefix = f'{no} '
        if text.upper().startswith(prefix.upper()):
            return _clean(text[len(prefix):])
        if text.upper() == no.upper():
            return None
    m = re.match(r'^([0-9][0-9A-Za-z]*(?:-[0-9][0-9A-Za-z]*)?)\s+(.+)$', text)
    if m:
        return _clean(m.group(2))
    return text


def _lookup_street_names(caddress, eaddress, street_no, street_index):
    """用 street_names.json 核對：命中則回 (chi, eng)，否則 (None, None)。

    除完整相等外，亦支援 nested 中文地址尾碼命中
    （例: 新界粉嶺沙頭角公路－龍躍頭段 → 沙頭角公路－龍躍頭段）。
    """
    chi_cand = _strip_chi_street_no(caddress, street_no)
    eng_cand = _strip_en_street_no(eaddress, street_no)

    for cand in (chi_cand, eng_cand):
        key = _normalize_street_key(cand)
        if key and key in street_index:
            return street_index[key]

    # 中文：用街名表後綴比對（nested 常帶 新界/粉嶺 前綴）
    chi_key = _normalize_street_key(chi_cand)
    if chi_key:
        best = None
        best_len = 0
        for key, pair in street_index.items():
            # 只比對中文 key（含 CJK）
            if not re.search(r'[\u4e00-\u9fff]', key):
                continue
            if chi_key.endswith(key) and len(key) > best_len:
                best = pair
                best_len = len(key)
        if best:
            return best

    return None, None


def _find_nested_facility_addresses(info):
    """從 facility[].addressInfo 找第一組非空 caddress/eaddress。"""
    facilities = info.get('facility') or []
    if not isinstance(facilities, list):
        return None, None
    for faci in facilities:
        if not isinstance(faci, dict):
            continue
        for nested in faci.get('addressInfo') or []:
            if not isinstance(nested, dict):
                continue
            caddr = _clean(nested.get('caddress'))
            eaddr = _clean(nested.get('eaddress'))
            if caddr or eaddr:
                return caddr, eaddr
    return None, None


def _strip_region_district_suffix_eng(eaddress, en_district=None, en_region=None):
    """去掉 nested 英文地址尾部 district/region。

    例: 2 Sha Tau Kok Road - Lung Yeuk Tau, Fanling, New Territories
      → 2 Sha Tau Kok Road - Lung Yeuk Tau
    """
    text = _clean(eaddress)
    if text is None:
        return None
    parts = [p.strip() for p in text.split(',') if p.strip()]
    drop_keys = {
        'new territories', 'kowloon', 'hong kong',
        'fanling', 'sheung shui', 'tai po', 'yuen long', 'tuen mun',
        'tsuen wan', 'sai kung', 'sha tin', 'tseung kwan o',
    }
    for p in (en_district, en_region):
        if p:
            drop_keys.add(p.casefold())
    while len(parts) > 1 and parts[-1].casefold() in drop_keys:
        parts.pop()
    return _clean(', '.join(parts)) or _clean(eaddress)


def _enrich_identify_addresses(info, entry):
    """補齊 caddress/eaddress：頂層優先，否則用 facility nested。"""
    caddress = _clean(info.get('caddress'))
    eaddress = _clean(info.get('eaddress'))
    if caddress and eaddress:
        return caddress, eaddress

    nested_c, nested_e = _find_nested_facility_addresses(info)
    if not caddress and nested_c:
        # 保留完整 nested 字串，街名比對用後綴命中
        caddress = nested_c
    if not eaddress and nested_e:
        en_region = None
        tc_region = _clean(entry.get('tc_region'))
        for code, name in TC_REGION.items():
            if name == tc_region:
                en_region = EN_REGION.get(code)
                break
        eaddress = _strip_region_district_suffix_eng(
            nested_e,
            _clean(entry.get('en_district')),
            en_region,
        )
    return caddress, eaddress


def _build_identify_update_row(entry, first_info, street_index):
    """依街名表核對結果，組出 Address_Flattened 更新內容。

    回傳 None 表示不更新（避免覆寫成只剩區）。
    """
    row_id = entry.get('id')
    ref_csuid = _clean(entry.get('ref_csuid'))
    tc_district = _clean(entry.get('tc_district'))
    en_district = _clean(entry.get('en_district'))
    tc_region = _clean(entry.get('tc_region'))
    en_region = None
    for code, name in TC_REGION.items():
        if name == tc_region:
            en_region = EN_REGION.get(code)
            break

    street_no = _clean(entry.get('tc_street_no')) or _clean(entry.get('en_street_no'))
    cname = _clean(first_info.get('cname'))
    ename = _clean(first_info.get('ename'))
    caddress, eaddress = _enrich_identify_addresses(first_info, entry)

    # caddress / cname 都無 → 唔更新（避免寫成只剩區）
    if not caddress and not cname and not eaddress and not ename:
        return None

    matched_chi, matched_eng = _lookup_street_names(
        caddress, eaddress, street_no, street_index,
    )
    is_street = bool(matched_chi or matched_eng)

    if is_street:
        # 在街名表內 → 填 street_name；樓宇用 cname/ename
        tc_street = matched_chi
        en_street = matched_eng
        out_street_no = street_no

        if tc_street and out_street_no:
            tc_street_and_no = f'{tc_street}{out_street_no}號'
        else:
            tc_street_and_no = caddress or (
                f'{out_street_no}號' if out_street_no else tc_street
            )

        if out_street_no and en_street:
            en_street_and_no = f'{out_street_no} {en_street}'
        else:
            en_street_and_no = eaddress or out_street_no or en_street

        tc_value = cname
        en_value = ename
        tc_label = ' '.join(p for p in [tc_street_and_no, cname] if p) or None
        en_label = ', '.join(p for p in [ename, en_street_and_no] if p) or None
        tc_full = ''.join(p for p in [tc_district, tc_label] if p) or None
        en_full = ', '.join(p for p in [en_label, en_district, en_region] if p) or None
    elif caddress or eaddress:
        # 有地址字串但不在街名表 → 不當 street，整段當 building
        tc_street = None
        en_street = None
        out_street_no = None

        tc_label = caddress
        en_label = eaddress
        tc_value = caddress
        en_value = eaddress
        tc_full = ''.join(p for p in [tc_district, tc_label] if p) or None
        en_full = ', '.join(p for p in [en_label, en_district, en_region] if p) or None
    else:
        # 無 caddress/eaddress，只有 cname/ename
        # label/value = 樓宇名；保留原 street_no；不填 street_name
        tc_street = None
        en_street = None
        out_street_no = street_no

        tc_label = cname
        en_label = ename
        tc_value = cname
        en_value = ename
        tc_full = ''.join(p for p in [tc_district, tc_label] if p) or None
        en_full = ', '.join(p for p in [en_label, en_district, en_region] if p) or None

        # 仍只剩區 → 唔更新
        if tc_full == tc_district and (en_full in (None, en_district, f'{en_district}, {en_region}')):
            return None

    return {
        'id': row_id,
        'ref_csuid': ref_csuid,
        'tc_street_name': tc_street,
        'tc_street_no': out_street_no,
        'tc_full_addr': tc_full,
        'tc_building_field_label': tc_label,
        'tc_building_field_value': tc_value,
        'sc_street_name': _to_sc(tc_street),
        'sc_street_no': _to_sc(out_street_no),
        'sc_full_addr': _to_sc(tc_full),
        'sc_building_field_label': _to_sc(tc_label),
        'sc_building_field_value': _to_sc(tc_value),
        'en_street_name': en_street,
        'en_street_no': out_street_no,
        'en_full_addr': en_full,
        'en_building_field_label': en_label,
        'en_building_field_value': en_value,
        '_matched_street': is_street,
    }


def apply_identify_json_to_address_table(engine, json_path=None):
    """將 Identify JSON 回填到 Address_Flattened（同表同欄位）。

    規則:
    1) identify.results 為空 → 刪除該列
    2) bdcsuid == ref_csuid 時：
       - 頂層 caddress/eaddress 為空時，從 facility nested 補
       - 用 street_names.json 核對；命中 → 填 street；cname/ename 填 building
       - 未命中但有 caddress → 當 building
       - 無 caddress 但有 cname → label/value=cname，保留 street_no
       - caddress 與 cname 都無 → 不更新
    """
    path = Path(json_path or IDENTIFY_RESULT_JSON)
    if not path.is_file():
        print(f'ℹ️ 找不到 Identify JSON，略過回填：{path}')
        return

    street_index = _load_street_name_index()
    data = json.loads(path.read_text(encoding='utf-8'))
    delete_rows = []
    update_rows = []
    matched_n = 0
    building_n = 0
    skipped_n = 0

    for entry in data:
        row_id = entry.get('id')
        ref_csuid = _clean(entry.get('ref_csuid'))
        if row_id is None or ref_csuid is None:
            continue

        first_result, first_info = _first_identify_address_info(entry)
        if first_result is None:
            delete_rows.append({'id': row_id, 'ref_csuid': ref_csuid})
            continue
        if first_info is None:
            continue

        bdcsuid = _clean(first_info.get('bdcsuid'))
        if bdcsuid != ref_csuid:
            continue

        row = _build_identify_update_row(entry, first_info, street_index)
        if row is None:
            skipped_n += 1
            continue
        if row.pop('_matched_street', False):
            matched_n += 1
        else:
            building_n += 1
        update_rows.append(row)

    with engine.begin() as conn:
        if delete_rows:
            conn.execute(
                text("""
                    DELETE FROM Address_Flattened
                    WHERE id = :id
                      AND ref_csuid = :ref_csuid
                """),
                delete_rows,
            )

        if update_rows:
            conn.execute(
                text("""
                    UPDATE Address_Flattened
                    SET
                        tc_street_name = :tc_street_name,
                        tc_street_no = :tc_street_no,
                        tc_full_addr = :tc_full_addr,
                        tc_building_field_label = :tc_building_field_label,
                        tc_building_field_value = :tc_building_field_value,
                        sc_street_name = :sc_street_name,
                        sc_street_no = :sc_street_no,
                        sc_full_addr = :sc_full_addr,
                        sc_building_field_label = :sc_building_field_label,
                        sc_building_field_value = :sc_building_field_value,
                        en_street_name = :en_street_name,
                        en_street_no = :en_street_no,
                        en_full_addr = :en_full_addr,
                        en_building_field_label = :en_building_field_label,
                        en_building_field_value = :en_building_field_value
                    WHERE id = :id
                      AND ref_csuid = :ref_csuid
                """),
                update_rows,
            )

    print(
        f'✅ Identify 回填完成：刪除 {len(delete_rows)} 筆，'
        f'更新 {len(update_rows)} 筆'
        f'（街名命中 {matched_n}，當 building {building_n}，略過 {skipped_n}）。'
    )


def clear_street_only_building_labels(engine):
    """若 building_field_label 只是 street_name + street_no，且 value 為空 → label 清成 NULL。

    中文例: label = 彌敦道12號、value IS NULL → label = NULL
    英文例: label = 12 NATHAN ROAD、value IS NULL → label = NULL
    """
    with engine.begin() as conn:
        tc_n = conn.execute(text("""
            UPDATE Address_Flattened
            SET tc_building_field_label = NULL
            WHERE (tc_building_field_value IS NULL OR tc_building_field_value = '')
              AND tc_building_field_label IS NOT NULL
              AND tc_building_field_label = CASE
                    WHEN tc_street_name IS NOT NULL AND tc_street_no IS NOT NULL
                      THEN tc_street_name || tc_street_no || '號'
                    WHEN tc_street_name IS NOT NULL
                      THEN tc_street_name
                    WHEN tc_street_no IS NOT NULL
                      THEN tc_street_no || '號'
                    ELSE NULL
                  END
        """)).rowcount

        sc_n = conn.execute(text("""
            UPDATE Address_Flattened
            SET sc_building_field_label = NULL
            WHERE (sc_building_field_value IS NULL OR sc_building_field_value = '')
              AND sc_building_field_label IS NOT NULL
              AND sc_building_field_label = CASE
                    WHEN sc_street_name IS NOT NULL AND sc_street_no IS NOT NULL
                      THEN sc_street_name || sc_street_no || '号'
                    WHEN sc_street_name IS NOT NULL
                      THEN sc_street_name
                    WHEN sc_street_no IS NOT NULL
                      THEN sc_street_no || '号'
                    ELSE NULL
                  END
        """)).rowcount

        en_n = conn.execute(text("""
            UPDATE Address_Flattened
            SET en_building_field_label = NULL
            WHERE (en_building_field_value IS NULL OR en_building_field_value = '')
              AND en_building_field_label IS NOT NULL
              AND en_building_field_label = CASE
                    WHEN en_street_name IS NOT NULL AND en_street_no IS NOT NULL
                      THEN en_street_no || ' ' || en_street_name
                    WHEN en_street_name IS NOT NULL
                      THEN en_street_name
                    WHEN en_street_no IS NOT NULL
                      THEN en_street_no
                    ELSE NULL
                  END
        """)).rowcount

    print(
        f'✅ 已清空「僅街名+門牌」的 building_field_label：'
        f'tc={tc_n}, sc={sc_n}, en={en_n}'
    )


def transform_to_output_schema(df: pd.DataFrame) -> pd.DataFrame:
    """把 MySQL 扁平結果轉成目標輸出欄位。"""
    rows = []
    identify_targets = []
    skipped_placeholder = 0
    skipped_district_only = 0
    for raw in df.to_dict(orient='records'):
        # 門牌為「*」會產出 *號，非有效地址，直接略過整列
        if _is_placeholder_building_no(raw.get('Building_No')):
            skipped_placeholder += 1
            continue

        region_code = _clean(raw.get('REGION'))
        tc_region = TC_REGION.get(region_code, region_code)
        sc_region = SC_REGION.get(region_code, _to_sc(region_code) if region_code else None)
        en_region = EN_REGION.get(region_code, region_code)

        tc_district, tc_street, tc_street_no, tc_full, tc_label, tc_value = _build_tc_parts(raw)
        en_district = _clean(raw.get('District_Name_Eng'))
        en_street, en_street_no, en_full, en_label, en_value = _build_en_parts(
            raw, en_region, en_district
        )

        # 有門牌、無街名 → 稍後呼叫 Identify API（即使最終不寫入主表也收集）
        if not tc_street and tc_street_no:
            identify_targets.append({
                'id': raw.get('ADDRESS2DID'),
                'ref_csuid': _clean(raw.get('REFCSUID')),
                'tc_region': tc_region,
                'tc_district': tc_district,
                'tc_street_no': tc_street_no,
                'tc_full_addr': tc_full,
                'tc_building_field_label': tc_label,
                'en_district': en_district,
                'en_street_no': en_street_no,
                'en_full_addr': en_full,
                'coordinates': _clean(raw.get('Coordinates')),
            })

        # 只有區名、無實際地址細節 → 略過
        if _is_district_only_addr(tc_district, tc_full, en_district, en_full, en_region):
            skipped_district_only += 1
            continue

        rows.append({
            'id': raw.get('ADDRESS2DID'),
            'ref_csuid': _clean(raw.get('REFCSUID')),
            'tc_region': tc_region,
            'tc_district': tc_district,
            'tc_street_name': tc_street,
            'tc_street_no': tc_street_no,
            'tc_full_addr': tc_full,
            'tc_building_field_label': tc_label,
            'tc_building_field_value': tc_value,
            'sc_region': sc_region,
            'sc_district': _to_sc(tc_district),
            'sc_street_name': _to_sc(tc_street),
            'sc_street_no': _to_sc(tc_street_no),
            'sc_full_addr': _to_sc(tc_full),
            'sc_building_field_label': _to_sc(tc_label),
            'sc_building_field_value': _to_sc(tc_value),
            'en_region': en_region,
            'en_district': en_district,
            'en_street_name': en_street,
            'en_street_no': en_street_no,
            'en_full_addr': en_full,
            'en_building_field_label': en_label,
            'en_building_field_value': en_value,
            'coordinates': _clean(raw.get('Coordinates')),
        })

    if skipped_placeholder:
        print(f'⚠️ 已略過門牌為「*」的無效地址 {skipped_placeholder} 筆。')
    if skipped_district_only:
        print(f'⚠️ 已略過僅有行政區、無實際地址的列 {skipped_district_only} 筆。')
    if identify_targets:
        print(f'📌 發現有門牌、無街名地址 {len(identify_targets)} 筆，稍後呼叫 Identify API。')

    out = pd.DataFrame(rows, columns=OUTPUT_COLUMNS)
    out['id'] = pd.to_numeric(out['id'], errors='coerce').astype('Int64')
    out.attrs['identify_targets'] = identify_targets
    # ref_csuid / REFCSUID 為文字，勿轉成整數

    # 只有「所有欄位完全相同」才算重複
    exact_dup_mask = out.duplicated(keep=False)
    exact_dups = out.loc[exact_dup_mask].sort_values(by=list(OUTPUT_COLUMNS))
    if not exact_dups.empty:
        group_n = exact_dups.drop_duplicates().shape[0]
        extra_n = len(out) - len(out.drop_duplicates(keep='first'))
        print(
            f"⚠️ 發現整列完全相同的重複資料："
            f"{group_n} 組不重複內容，將多移除 {extra_n} 筆。"
        )
        print("—— 重複列（整列相同）——")
        print("—— 以上 ——")
        out = out.drop_duplicates(keep='first')
    else:
        print("✅ 沒有整列完全相同的重複資料。")

    # 同 id 但其他欄位不同：不算「重複刪除」對象，但會影響 PRIMARY KEY
    id_conflict_mask = out.duplicated(subset=['id'], keep=False)
    id_conflicts = out.loc[id_conflict_mask].sort_values(by=['id'])
    if not id_conflicts.empty:
        conflict_ids = id_conflicts['id'].nunique()
        print(
            f"⚠️ 另有 {conflict_ids} 個 id 對應多筆「內容不完全相同」的列"
            f"（共 {len(id_conflicts)} 列）。這些不會被當重複刪除。"
        )
        print("—— 同 id、內容不同 ——")
        print("—— 以上 ——")
        out.attrs['id_unique'] = False
    else:
        out.attrs['id_unique'] = True

    return out


def space_cjk_for_fts(value):
    """在中文字之間插入空白，讓 unicode61 能以單字建立索引。"""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    text = str(value).strip()
    if not text:
        return None
    spaced = _CJK_RE.sub(r' \1 ', text)
    return re.sub(r'\s+', ' ', spaced).strip()


def to_fts_match_query(keyword: str) -> str:
    """把使用者關鍵字轉成 FTS5 MATCH 語法。

    範例:
      旺角      -> "旺 角"
      旺角 消防 -> "旺 角" AND "消 防"
      Mong      -> Mong*
    """
    parts = [p for p in str(keyword).strip().split() if p]
    clauses = []
    for part in parts:
        if _CJK_RE.search(part):
            spaced = space_cjk_for_fts(part)
            if spaced:
                clauses.append(f'"{spaced}"')
        else:
            clauses.append(f'{part}*')
    return ' AND '.join(clauses)


def _sqlserver_to_sqlite(sql: str) -> str:
    """把 SQL Server 風格 [ident] 轉成 SQLite 可用語句。"""
    return re.sub(r'\[([^\]]+)\]', r'\1', sql)


def write_sub_district_map_table(engine):
    """建立 sub_district_map 並匯入配套 SQL 資料。"""
    if not SUB_DISTRICT_MAP_DDL.is_file():
        raise FileNotFoundError(f'找不到 DDL: {SUB_DISTRICT_MAP_DDL}')
    if not SUB_DISTRICT_MAP_DATA.is_file():
        raise FileNotFoundError(f'找不到資料檔: {SUB_DISTRICT_MAP_DATA}')

    create_sql = _sqlserver_to_sqlite(SUB_DISTRICT_MAP_DDL.read_text(encoding='utf-8'))
    # seq 欄位在資料檔多為空字串 ''，改成 NULL 以符合 INT
    data_sql = _sqlserver_to_sqlite(SUB_DISTRICT_MAP_DATA.read_text(encoding='utf-8'))
    data_sql = re.sub(
        r"(VALUES\s*\([^)]*?),\s*''\s*,(\s*'[^']*'\s*\))",
        r'\1, NULL,\2',
        data_sql,
        flags=re.IGNORECASE,
    )

    with engine.begin() as conn:
        conn.execute(text('DROP TABLE IF EXISTS sub_district_map'))
        conn.execute(text(create_sql))
        for stmt in data_sql.split(';'):
            stmt = stmt.strip()
            if stmt:
                conn.execute(text(stmt))

    with engine.connect() as conn:
        n = conn.execute(text('SELECT COUNT(*) FROM sub_district_map')).scalar()
    print(f'✅ sub_district_map 已寫入 {n} 筆。')
    return n


def write_address_table(engine, df: pd.DataFrame):
    """寫入 Address_Flattened；id 唯一時設 PRIMARY KEY。"""
    id_unique = bool(df.attrs.get('id_unique', df['id'].is_unique))
    id_col_sql = 'id INTEGER PRIMARY KEY' if id_unique else 'id INTEGER'
    if not id_unique:
        print('⚠️ id 非唯一，建表時不設 PRIMARY KEY，以免寫入失敗。')

    create_sql = f"""
        CREATE TABLE Address_Flattened (
            {id_col_sql},
            ref_csuid TEXT,
            tc_region TEXT,
            tc_district TEXT,
            tc_street_name TEXT,
            tc_street_no TEXT,
            tc_full_addr TEXT,
            tc_building_field_label TEXT,
            tc_building_field_value TEXT,
            sc_region TEXT,
            sc_district TEXT,
            sc_street_name TEXT,
            sc_street_no TEXT,
            sc_full_addr TEXT,
            sc_building_field_label TEXT,
            sc_building_field_value TEXT,
            en_region TEXT,
            en_district TEXT,
            en_street_name TEXT,
            en_street_no TEXT,
            en_full_addr TEXT,
            en_building_field_label TEXT,
            en_building_field_value TEXT,
            coordinates TEXT
        )
    """
    with engine.begin() as conn:
        conn.execute(text('DROP TABLE IF EXISTS Address_FTS'))
        conn.execute(text('DROP TABLE IF EXISTS Address_Flattened'))
        conn.execute(text(create_sql))
    df.to_sql('Address_Flattened', engine, index=False, if_exists='append')


def create_address_indexes(engine):
    """按現有 Java 查詢路徑建立索引（不考慮空間，優先讀取效能）。"""
    index_sqls = [
        # Generic / join / ordering helpers
        "CREATE INDEX IF NOT EXISTS idx_af_id ON Address_Flattened(id)",
        "CREATE INDEX IF NOT EXISTS idx_af_ref_csuid_id ON Address_Flattened(ref_csuid, id)",
        "CREATE INDEX IF NOT EXISTS idx_af_region_cover ON Address_Flattened(tc_region, sc_region, en_region)",

        # Region + district filters (used heavily by dropdown APIs)
        "CREATE INDEX IF NOT EXISTS idx_af_tc_region_district ON Address_Flattened(tc_region, tc_district)",
        "CREATE INDEX IF NOT EXISTS idx_af_sc_region_district ON Address_Flattened(sc_region, sc_district)",
        "CREATE INDEX IF NOT EXISTS idx_af_en_region_district ON Address_Flattened(en_region, en_district)",

        # Street no ordering and filtering
        "CREATE INDEX IF NOT EXISTS idx_af_tc_region_district_streetno_sort ON Address_Flattened(tc_region, tc_district, CAST(tc_street_no AS INTEGER), tc_street_no)",
        "CREATE INDEX IF NOT EXISTS idx_af_sc_region_district_streetno_sort ON Address_Flattened(sc_region, sc_district, CAST(sc_street_no AS INTEGER), sc_street_no)",
        "CREATE INDEX IF NOT EXISTS idx_af_en_region_district_streetno_sort ON Address_Flattened(en_region, en_district, CAST(en_street_no AS INTEGER), en_street_no)",

        # Building list retrieval with street no + label sort
        "CREATE INDEX IF NOT EXISTS idx_af_tc_region_district_streetno_blabel ON Address_Flattened(tc_region, tc_district, tc_street_no, tc_building_field_label)",
        "CREATE INDEX IF NOT EXISTS idx_af_sc_region_district_streetno_blabel ON Address_Flattened(sc_region, sc_district, sc_street_no, sc_building_field_label)",
        "CREATE INDEX IF NOT EXISTS idx_af_en_region_district_streetno_blabel ON Address_Flattened(en_region, en_district, en_street_no, en_building_field_label)",

        # Field-unit lookup path (coordinates projection)
        "CREATE INDEX IF NOT EXISTS idx_af_tc_region_district_streetno_coord ON Address_Flattened(tc_region, tc_district, tc_street_no, coordinates)",
        "CREATE INDEX IF NOT EXISTS idx_af_sc_region_district_streetno_coord ON Address_Flattened(sc_region, sc_district, sc_street_no, coordinates)",
        "CREATE INDEX IF NOT EXISTS idx_af_en_region_district_streetno_coord ON Address_Flattened(en_region, en_district, en_street_no, coordinates)",

        # Matched-address API: region + en_district + output ordering
        "CREATE INDEX IF NOT EXISTS idx_af_tc_region_en_district_ref ON Address_Flattened(tc_region, en_district, ref_csuid, id)",
        "CREATE INDEX IF NOT EXISTS idx_af_sc_region_en_district_ref ON Address_Flattened(sc_region, en_district, ref_csuid, id)",
        "CREATE INDEX IF NOT EXISTS idx_af_en_region_en_district_ref ON Address_Flattened(en_region, en_district, ref_csuid, id)",

        # Exact-match helper indexes for validator / translation OR predicates
        "CREATE INDEX IF NOT EXISTS idx_af_tc_street_name ON Address_Flattened(tc_street_name)",
        "CREATE INDEX IF NOT EXISTS idx_af_sc_street_name ON Address_Flattened(sc_street_name)",
        "CREATE INDEX IF NOT EXISTS idx_af_en_street_name ON Address_Flattened(en_street_name)",
        "CREATE INDEX IF NOT EXISTS idx_af_tc_building_value ON Address_Flattened(tc_building_field_value)",
        "CREATE INDEX IF NOT EXISTS idx_af_sc_building_value ON Address_Flattened(sc_building_field_value)",
        "CREATE INDEX IF NOT EXISTS idx_af_en_building_value ON Address_Flattened(en_building_field_value)",
        "CREATE INDEX IF NOT EXISTS idx_af_tc_street_no ON Address_Flattened(tc_street_no)",
        "CREATE INDEX IF NOT EXISTS idx_af_sc_street_no ON Address_Flattened(sc_street_no)",
        "CREATE INDEX IF NOT EXISTS idx_af_en_street_no ON Address_Flattened(en_street_no)",
    ]

    with engine.begin() as conn:
        for sql in index_sqls:
            conn.execute(text(sql))


def build_fts5_index(engine):
    """建立 Address_FTS (FTS5) 虛擬表。"""
    with engine.begin() as conn:
        conn.execute(text('DROP TABLE IF EXISTS Address_FTS'))
        conn.execute(text("""
            CREATE VIRTUAL TABLE Address_FTS USING fts5(
                id UNINDEXED,
                tc_district,
                tc_street_name,
                tc_full_addr,
                tc_building_field_label,
                tc_building_field_value,
                sc_district,
                sc_street_name,
                sc_full_addr,
                sc_building_field_label,
                sc_building_field_value,
                en_district,
                en_street_name,
                en_full_addr,
                en_building_field_label,
                en_building_field_value,
                tokenize = 'unicode61 remove_diacritics 2'
            )
        """))

        src = pd.read_sql(
            f"SELECT id, {', '.join(_FTS_TEXT_COLS)} FROM Address_Flattened",
            conn,
        )
        src['id'] = pd.to_numeric(src['id'], errors='coerce').astype('Int64')
        for col in _FTS_TEXT_COLS:
            src[col] = src[col].map(space_cjk_for_fts)

        cols = ['id', *_FTS_TEXT_COLS]
        placeholders = ', '.join(f':{c}' for c in cols)
        insert_sql = text(
            f"INSERT INTO Address_FTS ({', '.join(cols)}) VALUES ({placeholders})"
        )
        rows = src.where(pd.notnull(src), None).to_dict(orient='records')
        for i in range(0, len(rows), 1000):
            conn.execute(insert_sql, rows[i:i + 1000])


def verify_sqlite_db(engine, expect_rows=None):
    """驗證產生的 SQLite：表、欄位、筆數、中文拆字、FTS MATCH、JOIN。"""
    db_path = engine.url.database
    print("\n🧪 開始驗證 SQLite 資料庫...")
    print(f"   檔案路徑: {db_path}")

    results = []

    def check(name, ok, detail=''):
        status = 'PASS' if ok else 'FAIL'
        results.append(ok)
        suffix = f' — {detail}' if detail else ''
        print(f"   [{status}] {name}{suffix}")

    with engine.connect() as conn:
        tables = {
            row[0]
            for row in conn.execute(
                text("SELECT name FROM sqlite_master WHERE type IN ('table', 'view')")
            )
        }
        check('存在 Address_Flattened', 'Address_Flattened' in tables)
        check('存在 Address_FTS', 'Address_FTS' in tables)

        cols = [
            row[1]
            for row in conn.execute(text('PRAGMA table_info(Address_Flattened)'))
        ]
        missing = [c for c in OUTPUT_COLUMNS if c not in cols]
        check('主表欄位完整', not missing, f'missing={missing}' if missing else 'ok')

        flat_n = conn.execute(text('SELECT COUNT(*) FROM Address_Flattened')).scalar()
        fts_n = conn.execute(text('SELECT COUNT(*) FROM Address_FTS')).scalar()
        check('主表筆數 > 0', flat_n > 0, f'Address_Flattened={flat_n}')
        check('FTS 筆數與主表一致', flat_n == fts_n, f'FTS={fts_n}')
        if expect_rows is not None:
            check('筆數與本次匯入一致', flat_n == expect_rows, f'expect={expect_rows}')

        sample = conn.execute(text("""
            SELECT
                a.id,
                a.tc_full_addr AS flat_addr,
                f.tc_full_addr AS fts_addr,
                length(a.tc_full_addr) AS flat_len,
                length(f.tc_full_addr) AS fts_len,
                instr(f.tc_full_addr, ' ') AS fts_space_pos,
                a.tc_region,
                a.sc_region,
                a.en_region
            FROM Address_Flattened a
            JOIN Address_FTS f ON a.id = f.id
            WHERE a.tc_full_addr IS NOT NULL
              AND length(a.tc_full_addr) >= 6
            LIMIT 20
        """)).mappings().all()

        sample = next(
            (
                row for row in sample
                if re.search(r'[\u4e00-\u9fff]{2,}', row['flat_addr'] or '')
            ),
            None,
        )

        if sample is None:
            check('抽樣中文地址', False, '找不到可抽樣的中文地址')
        else:
            flat_addr = sample['flat_addr'] or ''
            fts_addr = sample['fts_addr'] or ''
            flat_has_cjk_run = bool(re.search(r'[\u4e00-\u9fff]{2,}', flat_addr))
            fts_spaced = sample['fts_space_pos'] > 0 and bool(
                re.search(r'[\u4e00-\u9fff] [\u4e00-\u9fff]', fts_addr)
            )
            check(
                '主表保留原文（含連續中文）',
                flat_has_cjk_run and flat_addr != fts_addr,
                f'id={sample["id"]} flat={flat_addr!r}',
            )
            check(
                'FTS 已中文拆字',
                fts_spaced and sample['fts_len'] > sample['flat_len'],
                f'fts={fts_addr!r} len {sample["flat_len"]}→{sample["fts_len"]}',
            )
            check(
                'REGION 對照正常',
                sample['tc_region'] in TC_REGION.values()
                and sample['sc_region'] in SC_REGION.values()
                and sample['en_region'] in EN_REGION.values(),
                f'tc={sample["tc_region"]} sc={sample["sc_region"]} en={sample["en_region"]}',
            )

        phrase_q = to_fts_match_query('旺角')
        match_phrase = conn.execute(
            text('SELECT COUNT(*) FROM Address_FTS WHERE Address_FTS MATCH :q'),
            {'q': phrase_q},
        ).scalar()
        match_raw = conn.execute(
            text("SELECT COUNT(*) FROM Address_FTS WHERE Address_FTS MATCH '旺角'")
        ).scalar()
        check(f'FTS MATCH {phrase_q!r} 有結果', match_phrase > 0, f'hits={match_phrase}')
        check(
            "FTS MATCH '旺角' 應近乎 0（未拆字語法）",
            match_raw == 0,
            f'hits={match_raw}',
        )

        join_n = conn.execute(text("""
            SELECT COUNT(*)
            FROM Address_FTS f
            JOIN Address_Flattened a ON a.id = f.id
            WHERE Address_FTS MATCH :q
        """), {'q': phrase_q}).scalar()
        orphan_n = conn.execute(text("""
            SELECT COUNT(*)
            FROM Address_FTS f
            WHERE Address_FTS MATCH :q
              AND NOT EXISTS (
                  SELECT 1 FROM Address_Flattened a WHERE a.id = f.id
              )
        """), {'q': phrase_q}).scalar()
        check(
            'FTS JOIN 主表成功',
            join_n > 0 and orphan_n == 0,
            f'join_hits={join_n}, orphans={orphan_n}',
        )

        conn.execute(text('DROP TABLE IF EXISTS _verify_fts_vocab'))
        conn.execute(text(
            "CREATE VIRTUAL TABLE _verify_fts_vocab USING fts5vocab(Address_FTS, 'row')"
        ))
        vocab = dict(
            conn.execute(text("""
                SELECT term, cnt FROM _verify_fts_vocab
                WHERE term IN ('旺', '角', '旺角')
            """)).fetchall()
        )
        conn.execute(text('DROP TABLE IF EXISTS _verify_fts_vocab'))
        check("詞彙含單字 '旺'", vocab.get('旺', 0) > 0, f"cnt={vocab.get('旺', 0)}")
        check("詞彙含單字 '角'", vocab.get('角', 0) > 0, f"cnt={vocab.get('角', 0)}")
        check("詞彙不含 '旺角'", vocab.get('旺角', 0) == 0, f"cnt={vocab.get('旺角', 0)}")

    passed = sum(1 for ok in results if ok)
    total = len(results)
    all_ok = all(results)
    print(f"\n🧪 驗證結束: {passed}/{total} 通過")
    if all_ok:
        print('✅ SQLite 資料庫驗證通過。')
    else:
        print('❌ SQLite 資料庫驗證失敗，請檢查上方 FAIL 項目。')
    return all_ok


def run_sync_and_verify(skip_identify_api: bool = False):
    print("🔄 正在連接 MySQL 並執行複雜查詢，這可能需要幾分鐘 (取決於資料量)...")
    raw_df = pd.read_sql(sql_query, mysql_engine)
    print(f"✅ 成功從 MySQL 讀取 {len(raw_df)} 筆地址資料！")

    print("🧩 正在轉換為 tc/sc/en 輸出結構...")
    df = transform_to_output_schema(raw_df)
    print(f"✅ 轉換完成，準備寫入 {len(df)} 筆。")

    print("💾 正在將資料寫入 SQLite (adi_address.sqlite)...")
    write_address_table(sqlite_engine, df)

    print("⚙️ 正在建立 Address_Flattened 索引...")
    create_address_indexes(sqlite_engine)

    print("🗺️ 正在建立 sub_district_map 並匯入資料...")
    write_sub_district_map_table(sqlite_engine)

    # 有門牌、無街名 → Identify API，結果寫獨立 JSON
    identify_targets = df.attrs.get('identify_targets') or []
    if skip_identify_api:
        if IDENTIFY_RESULT_JSON.is_file():
            print(
                f'⏭️ 已指定跳過 Identify API，沿用現有 {IDENTIFY_RESULT_JSON.name}'
            )
        else:
            print(
                f'⚠️ 已指定跳過 Identify API，但找不到 {IDENTIFY_RESULT_JSON.name}，'
                f'將略過回填。'
            )
    else:
        fetch_identify_for_missing_streets(identify_targets, IDENTIFY_RESULT_JSON)

    apply_identify_json_to_address_table(sqlite_engine, IDENTIFY_RESULT_JSON)
    clear_street_only_building_labels(sqlite_engine)

    print("🔍 正在建立 FTS5 虛擬表 Address_FTS...")
    build_fts5_index(sqlite_engine)

    example_q = to_fts_match_query('旺角')
    print("🎉 轉移完美結束！您的 SQLite 資料庫已準備就緒。")
    print("   - Address_Flattened : tc/sc/en 地址輸出表")
    print("   - Address_FTS       : FTS5 全文搜尋虛擬表（中文已拆字）")
    print("   - sub_district_map  : 分區／小區對照表")
    print(f"   - Identify JSON    : {IDENTIFY_RESULT_JSON.name}")
    print("   查詢範例:")
    print("   SELECT a.* FROM Address_FTS f")
    print("   JOIN Address_Flattened a ON a.id = f.id")
    print(f"   WHERE Address_FTS MATCH '{example_q}';")
    verified = verify_sqlite_db(sqlite_engine, expect_rows=len(df))

    print("\n🔎 tc_street_name 含空格清單：")
    with sqlite_engine.connect() as conn:
        rows = conn.execute(text("""
            SELECT DISTINCT tc_street_name
            FROM Address_Flattened
            WHERE tc_street_name IS NOT NULL
              AND tc_street_name LIKE '% %'
            ORDER BY tc_street_name
        """)).fetchall()
    if not rows:
        print("   (沒有符合條件的 street name)")
    else:
        for row in rows:
            print(f"   - {row[0]}")

    print_strange_street_nos(sqlite_engine)

    return verified


if __name__ == '__main__':
    import sys

    args = set(sys.argv[1:])
    if args & {'--verify', '--verify-only'}:
        raise SystemExit(0 if verify_sqlite_db(sqlite_engine) else 1)

    skip_identify_api = bool(
        args & {'--skip-identify-api', '--skip-identify'}
    )
    if skip_identify_api:
        print('ℹ️ 參數：--skip-identify-api（不呼叫 Identify API）')

    raise SystemExit(
        0 if run_sync_and_verify(skip_identify_api=skip_identify_api) else 1
    )
