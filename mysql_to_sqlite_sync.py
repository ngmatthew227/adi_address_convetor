import re
from pathlib import Path

import pandas as pd
from sqlalchemy import create_engine, text

try:
    import zhconv
except ImportError as exc:  # pragma: no cover
    raise ImportError(
        "需要 zhconv 做繁簡轉換，請先執行: pip install zhconv"
    ) from exc

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
            IFNULL(a.BUILDINGNUMEXT, ''),
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


def _clean(value):
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    text = str(value).strip()
    return text or None


_BUILDING_NO_RANGE_RE = re.compile(
    r'^([0-9]+[A-Za-z]*)-([0-9]+[A-Za-z]*)$'
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


def _build_tc_parts(row):
    district = _clean(row.get('District_Name_Chi'))
    street = _clean(row.get('Street_Full_Name_Chi'))
    street_no = _normalize_building_no(row.get('Building_No'))
    estate = _clean(row.get('Estate_Name_Chi'))
    phase = _clean(row.get('Phase_Name_Chi'))
    building = _clean(row.get('Building_Name_Chi'))

    no_with_unit = f'{street_no}號' if street_no else None

    # estate/phase/building 片段
    estate_part = None
    if estate or phase or building:
        estate_part = estate or ''
        if phase:
            estate_part = f'{estate_part} {phase}' if estate_part else phase
        if building:
            estate_part = f'{estate_part} {building}' if estate_part else building

    # label: 不含 region / district = Street + No號 + estate_part
    label = ''.join(p for p in [street, no_with_unit, estate_part] if p) or None

    # value:
    # - 一般: 不含 region / district / street / street_no (= estate_part)
    # - 鄉村: 不含 region / district (= label)
    if _is_village(row):
        value = label
    else:
        value = estate_part

    # full: district + label（舊格式不含 region 字面）
    full = ''.join(p for p in [district, label] if p) or None
    return district, street, street_no, full, label, value


def _build_en_parts(row, en_region, en_district):
    street = _clean(row.get('Street_Full_Name_Eng'))
    street_no = _normalize_building_no(row.get('Building_No'))
    estate = _clean(row.get('Estate_Name_Eng'))
    phase = _clean(row.get('Phase_Name_Eng'))
    building = _clean(row.get('Building_Name_Eng'))

    estate_part = ', '.join(p for p in [building, phase, estate] if p) or None

    if street_no and street:
        street_part = f'{street_no} {street}'
    else:
        street_part = street_no or street

    # label: 不含 region / district
    label = ', '.join(p for p in [building, phase, estate, street_part] if p) or None

    # value: 鄉村保留 street/no；一般則不含
    if _is_village(row):
        value = label
    else:
        value = estate_part

    # full: label + district + region
    full = ', '.join(p for p in [label, en_district, en_region] if p) or None
    return street, street_no, full, label, value


def transform_to_output_schema(df: pd.DataFrame) -> pd.DataFrame:
    """把 MySQL 扁平結果轉成目標輸出欄位。"""
    rows = []
    for raw in df.to_dict(orient='records'):
        region_code = _clean(raw.get('REGION'))
        tc_region = TC_REGION.get(region_code, region_code)
        sc_region = SC_REGION.get(region_code, _to_sc(region_code) if region_code else None)
        en_region = EN_REGION.get(region_code, region_code)

        tc_district, tc_street, tc_street_no, tc_full, tc_label, tc_value = _build_tc_parts(raw)
        en_district = _clean(raw.get('District_Name_Eng'))
        en_street, en_street_no, en_full, en_label, en_value = _build_en_parts(
            raw, en_region, en_district
        )

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

    out = pd.DataFrame(rows, columns=OUTPUT_COLUMNS)
    out['id'] = pd.to_numeric(out['id'], errors='coerce').astype('Int64')
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
        print(exact_dups[['id', 'tc_full_addr']].to_string(index=False))
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
        print(id_conflicts[['id', 'tc_full_addr']].to_string(index=False))
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


def run_sync_and_verify():
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

    print("🔍 正在建立 FTS5 虛擬表 Address_FTS...")
    build_fts5_index(sqlite_engine)

    print("🗺️ 正在建立 sub_district_map 並匯入資料...")
    write_sub_district_map_table(sqlite_engine)

    example_q = to_fts_match_query('旺角')
    print("🎉 轉移完美結束！您的 SQLite 資料庫已準備就緒。")
    print("   - Address_Flattened : tc/sc/en 地址輸出表")
    print("   - Address_FTS       : FTS5 全文搜尋虛擬表（中文已拆字）")
    print("   - sub_district_map  : 分區／小區對照表")
    print("   查詢範例:")
    print("   SELECT a.* FROM Address_FTS f")
    print("   JOIN Address_Flattened a ON a.id = f.id")
    print(f"   WHERE Address_FTS MATCH '{example_q}';")
    return verify_sqlite_db(sqlite_engine, expect_rows=len(df))


if __name__ == '__main__':
    import sys

    if len(sys.argv) > 1 and sys.argv[1] in ('--verify', '--verify-only'):
        raise SystemExit(0 if verify_sqlite_db(sqlite_engine) else 1)

    raise SystemExit(0 if run_sync_and_verify() else 1)
