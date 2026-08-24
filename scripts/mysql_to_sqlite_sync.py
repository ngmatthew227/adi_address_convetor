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
        "zhconv is required for Traditional/Simplified conversion; please run: pip install zhconv"
    ) from exc

try:
    from pyproj import Transformer
except ImportError:  # pragma: no cover
    Transformer = None

# ==========================================
# MySQL to SQLite auto-migration script (clean version)
# ==========================================
# Configuration is read from config.json (or overridden via CLI arguments).

DEFAULT_CONFIG = {
    "mysql": {
        "host": "127.0.0.1",
        "port": 3306,
        "user": "root",
        "password": "secretP@ssw0rd",
        "database": "EADI100",
    },
    "sqlite": {
        "path": "adi_address.sqlite",
    },
    "proxy": {
        "enabled": True,
        "url": "http://proxy1.scig.gov.hk:8080",
    },
}

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
CONFIG_PATH = PROJECT_ROOT / "config.json"
DATA_DIR = PROJECT_ROOT / "data"
REFERENCE_DIR = DATA_DIR / "reference"
OUTPUT_DIR = DATA_DIR / "output"

# Module-level SQLite engine; initialized from config in __main__.
sqlite_engine = None


def load_config(config_path: Path) -> dict:
    """Load configuration from a JSON file, falling back to defaults."""
    config = json.loads(json.dumps(DEFAULT_CONFIG))  # deep copy defaults
    if config_path.exists():
        with open(config_path, "r", encoding="utf-8") as fh:
            user_cfg = json.load(fh)
        _deep_merge(config, user_cfg)
    return config


def _deep_merge(base: dict, override: dict) -> None:
    """Recursively merge override into base (in place)."""
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _deep_merge(base[key], value)
        else:
            base[key] = value


def build_mysql_engine(cfg: dict):
    """Build the MySQL engine from config.

    User/password are URL-encoded so special characters (e.g. '@', '/', ':')
    in credentials do not break the SQLAlchemy connection URL.
    """
    mysql = cfg["mysql"]
    user = urllib.parse.quote(mysql["user"], safe="")
    password = urllib.parse.quote(mysql["password"], safe="")
    return create_engine(
        f"mysql+pymysql://{user}:{password}"
        f"@{mysql['host']}:{mysql['port']}/{mysql['database']}"
    )


def build_sqlite_engine(cfg: dict):
    """Build the SQLite engine from config."""
    path = Path(cfg["sqlite"]["path"])
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    return create_engine(f"sqlite:///{path}")


SUB_DISTRICT_MAP_DDL = REFERENCE_DIR / "sub_district_map.sql"
SUB_DISTRICT_MAP_DATA = REFERENCE_DIR / "sub_district_map_data.sql"
IDENTIFY_RESULT_JSON = OUTPUT_DIR / "missing_street_identify.json"
STREET_NAMES_JSON = REFERENCE_DIR / "street_names.json"
UNOFFICIAL_TC_STREETS_JSON = OUTPUT_DIR / "unofficial_tc_streets.json"
UNOFFICIAL_TC_STREETS_CLASSIFIED_JSON = (
    OUTPUT_DIR / "unofficial_tc_streets_classified.json"
)
IDENTIFY_API_URL = "https://www.map.gov.hk/gs/api/v1.0.0/identify"
GEODETIC_TRANSFORM_URL = "https://www.geodetic.gov.hk/transform/v2/"
IDENTIFY_REQUEST_INTERVAL_SEC = 0.35

# SQL statement with all hidden characters and illegal whitespace removed
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
            -- EXT concatenation rules:
            -- 1) If it already equals the trailing char of FROMALPHA, do not append (e.g. A1 + 1 -> do not append 1)
            -- 2) If it equals TO_A, do not append to the start number (e.g. 8 + EXT10 + TO 10 -> 8-10, not 810-10)
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

TC_REGION = {"NT": "新界", "KLN": "九龍", "HK": "香港"}
SC_REGION = {"NT": "新界", "KLN": "九龙", "HK": "香港"}
EN_REGION = {"NT": "NEW TERRITORIES", "KLN": "KOWLOON", "HK": "HONG KONG"}
OUTPUT_COLUMNS = [
    "id",
    "ref_csuid",
    "tc_region",
    "tc_district",
    "tc_street_name",
    "tc_street_no",
    "tc_full_addr",
    "tc_building_field_label",
    "tc_building_field_value",
    "sc_region",
    "sc_district",
    "sc_street_name",
    "sc_street_no",
    "sc_full_addr",
    "sc_building_field_label",
    "sc_building_field_value",
    "en_region",
    "en_district",
    "en_street_name",
    "en_street_no",
    "en_full_addr",
    "en_building_field_label",
    "en_building_field_value",
    "coordinates",
]

_CJK_RE = re.compile(r"([\u4e00-\u9fff])")
_FTS_TEXT_COLS = (
    "tc_district",
    "tc_street_name",
    "tc_full_addr",
    "tc_building_field_label",
    "tc_building_field_value",
    "sc_district",
    "sc_street_name",
    "sc_full_addr",
    "sc_building_field_label",
    "sc_building_field_value",
    "en_district",
    "en_street_name",
    "en_full_addr",
    "en_building_field_label",
    "en_building_field_value",
)


_STREET_CHI_PATCH = {
    "CHUT SHUI WAN": "出水灣",
    "MUK WO STREET": "沐和街",
    "OLYMPIC AVENUE": "世運道",
}


def _clean(value):
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    text = str(value).strip()
    return text or None


_BUILDING_NO_RANGE_RE = re.compile(r"^([0-9][0-9A-Za-z]*)-([0-9][0-9A-Za-z]*)$")
_BUILDING_NO_SLASH_RANGE_RE = re.compile(
    r"^([0-9]+[A-Za-z]*)/([0-9A-Za-z]+)-([0-9]+[A-Za-z]*)$"
)
_BUILDING_DIGITS_RE = re.compile(r"^([0-9]+)")


def _normalize_building_no(value):
    """Fix building numbers where the start/end range was wrongly concatenated as "{from}{to}-{to}".

    Examples:
      1819-19     -> 18-19
      114117-117  -> 114-117
      125A125-125 -> 125A-125
      8898-98     -> 88-98

    Logic: if the left segment ends with the right segment, and removing the right
    segment still looks like a building number with a reasonable digit count
    (right segment digits not longer than left), fix it. Avoids false positives
    like 120-20.
    """
    text = _clean(value)
    if text is None:
        return None

    # OFF in source data is usually an abbreviation marker for "off <street>", not a valid number; convert to empty.
    if text.upper() == "OFF":
        return None

    # "*" is a source placeholder number (would become *號), not a valid address; the whole row is dropped upstream.
    if text == "*":
        return None

    # Fix a/b-b and a/b-c types (including letters)
    # Examples:
    #   1/13-13     -> 1-13
    #   1/1A-1A     -> 1-1A
    #   103F/103H-103H -> 103F-103H
    #   123A/B-123B -> 123A-123B
    slash_m = _BUILDING_NO_SLASH_RANGE_RE.match(text)
    if slash_m:
        left, mid, right = slash_m.group(1), slash_m.group(2), slash_m.group(3)
        if mid == right:
            return f"{left}-{right}"
        # Allow the middle segment to provide only the right segment's suffix (e.g. B -> 123B)
        if right.endswith(mid):
            left_digits = _BUILDING_DIGITS_RE.match(left)
            right_digits = _BUILDING_DIGITS_RE.match(right)
            if (
                left_digits
                and right_digits
                and left_digits.group(1) == right_digits.group(1)
            ):
                return f"{left}-{right}"

    m = _BUILDING_NO_RANGE_RE.match(text)
    if not m:
        return text

    left, right = m.group(1), m.group(2)
    if left == right or not left.endswith(right):
        return text

    cand = left[: -len(right)]
    if not cand or not re.match(r"^[0-9]+[A-Za-z]*$", cand):
        return text

    left_digits = _BUILDING_DIGITS_RE.match(cand)
    right_digits = _BUILDING_DIGITS_RE.match(right)
    if not left_digits or not right_digits:
        return text
    # Start number digit count should be >= end number (when wrongly concatenated the lengths are similar; 120-20 -> cand=1 is blocked)
    if len(left_digits.group(1)) < len(right_digits.group(1)):
        return text

    return f"{cand}-{right}"


# Single street number segment: 1-5 digits (no leading zero) + at most 1 English letter
_STREET_NO_PART_RE = re.compile(r"^([1-9][0-9]{0,4})([A-Za-z]?)$")


def _is_normal_street_no(value) -> bool:
    """Determine whether a street number is "normal".

    Normal examples: 12 / 12A / 12-14 / 7A-7C
    Abnormal examples: 012 / 12AB / 181919 / 7C-7A / 1/13 / LOT / 12-12
    """
    text = _clean(value)
    if text is None:
        return True  # empty value is not an abnormal number format

    # Single segment or start/end two segments
    parts = text.split("-")
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

    # Start/end identical (whole segment equal) is unreasonable, e.g. 12-12 / 12A-12A
    if left_num == right_num and left_letter == right_letter:
        return False

    # Start number must not be greater than end number; when equal, letters must increase (A < C)
    if left_num > right_num:
        return False
    if left_num == right_num and left_letter >= right_letter:
        return False

    return True


def print_strange_street_nos(engine):
    """List tc_street_no values that still do not look like normal numbers after normalization (with counts)."""
    print("\n🔎 Abnormal tc_street_no list (strict rules):")
    print(
        "   Normal: 1-5 digits (no leading zero) + at most 1 letter; start/end must increase"
    )
    print("   Normal examples: 12 / 12A / 12-14 / 7A-7C")
    with engine.connect() as conn:
        rows = conn.execute(text("""
            SELECT tc_street_no, COUNT(*) AS cnt
            FROM Address_Flattened
            WHERE tc_street_no IS NOT NULL
              AND tc_street_no != ''
            GROUP BY tc_street_no
            ORDER BY cnt DESC, tc_street_no
        """)).fetchall()

    strange = [(no, cnt) for no, cnt in rows if not _is_normal_street_no(no)]
    if not strange:
        print("   (no abnormal street_no)")
        return

    print(
        f"   {len(strange)} abnormal values in total (total rows {sum(c for _, c in strange)}):"
    )
    for no, cnt in strange:
        print(f"   - {no!r}  ({cnt})")


def _to_sc(value):
    text = _clean(value)
    if text is None:
        return None
    return zhconv.convert(text, "zh-cn")


# Based on STREETNAME.CHITYPE / ENGTYPE actual enum: village/array-village type whitelist
_HK_VILLAGE_TYPES_CHI = frozenset(
    {
        "村",
        "圍",
        "新村",
        "下村",
        "舊村",
        "上村",
        "中村",
        "北村",
        "東村",
        "西村",
        "南村",
        "後村",
        "舊圍",
        "新圍",
        "遷建村",
    }
)
_HK_VILLAGE_TYPES_ENG = frozenset(
    {
        "wai",
        "tsuen",
        "village",
        "new village",
        "ha tsuen",
        "resite village",
        "resited village",
        "kau tsuen",
        "san tsuen",
        "sheung tsuen",
        "chung tsuen",
        "north tsuen",
        "north village",
        "upper village",
        "kau wai",
        "east village",
        "east tsuen",
        "san wai",
        "back tsuen",
        "south tsuen",
        "west tsuen",
        "west village",
    }
)

# When type is NULL, use the full-name suffix as fallback (avoids "廈村路" - which has type "路" and is excluded above)
_HK_VILLAGE_SUFFIX_CHI = tuple(_HK_VILLAGE_TYPES_CHI)
_HK_VILLAGE_SUFFIX_ENG = re.compile(
    r"(?:^|[\s\-])(?:"
    r"walled\s+village|resite[d]?\s+village|upper\s+village|"
    r"new\s+village|east\s+village|west\s+village|north\s+village|"
    r"ha\s+tsuen|kau\s+tsuen|san\s+tsuen|sheung\s+tsuen|chung\s+tsuen|"
    r"north\s+tsuen|east\s+tsuen|south\s+tsuen|west\s+tsuen|back\s+tsuen|"
    r"kau\s+wai|san\s+wai|village|tsuen|wai"
    r")$",
    re.IGNORECASE,
)


def _is_village(row) -> bool:
    """Determine whether a row is a village/array-village number based on the HK STREETNAME type enum.

    Rules:
    1. CHITYPE / ENGTYPE in the village whitelist -> yes
       (includes: 村, 圍, 新村, 遷建村, TSUEN, VILLAGE, WAI...)
    2. Has a non-empty type not in the whitelist -> no
       (includes: 街, 路, 道, 邨, ESTATE, RD, ST..., avoids "廈村路")
    3. Only when type is NULL, use the Chinese/English full-name suffix as fallback
    """
    type_chi = _clean(row.get("Street_Type_Chi")) or ""
    type_eng = (_clean(row.get("Street_Type_Eng")) or "").casefold().strip()
    street_chi = _clean(row.get("Street_Full_Name_Chi")) or ""
    street_eng = _clean(row.get("Street_Full_Name_Eng")) or ""

    # 1) Official type whitelist
    if type_chi in _HK_VILLAGE_TYPES_CHI:
        return True
    if type_eng in _HK_VILLAGE_TYPES_ENG:
        return True

    # 2) Has an explicit non-village type -> not a village
    if type_chi or type_eng:
        return False

    # 3) Type NULL: name suffix fallback
    if street_chi.endswith(_HK_VILLAGE_SUFFIX_CHI):
        return True
    if street_eng and _HK_VILLAGE_SUFFIX_ENG.search(street_eng.strip()):
        return True

    return False


def _street_display_name_chi(row) -> str | None:
    """Chinese street display name: street name + type (e.g. 坪洋新+村, 彌敦+道); if the type suffix is already present, do not append it again."""
    street = _clean(row.get("Street_Full_Name_Chi"))
    if street:
        street = _STREET_CHI_PATCH.get(street.upper(), street)
    type_chi = _clean(row.get("Street_Type_Chi"))
    if street and type_chi:
        if street.endswith(type_chi):
            return street
        return f"{street}{type_chi}"
    return street or type_chi


def _street_display_name_eng(row) -> str | None:
    """English street display name: street name + type (e.g. PING YEUNG NEW + VILLAGE); if the type suffix is already present, do not append it again."""
    street = _clean(row.get("Street_Full_Name_Eng"))
    type_eng = _clean(row.get("Street_Type_Eng"))
    if street and type_eng:
        if street.casefold().endswith(type_eng.casefold()):
            return street
        return f"{street} {type_eng}"
    return street or type_eng


def _build_tc_parts(row):
    district = _clean(row.get("District_Name_Chi"))
    street_no = _normalize_building_no(row.get("Building_No"))
    estate = _clean(row.get("Estate_Name_Chi"))
    phase = _clean(row.get("Phase_Name_Chi"))
    building = _clean(row.get("Building_Name_Chi"))
    is_village = _is_village(row)

    # Always append the type (村/道/街...); villages do not write to the street field
    display_street = _street_display_name_chi(row)
    if is_village:
        out_street = None
        out_street_no = None
    else:
        out_street = display_street
        out_street_no = street_no

    no_with_unit = f"{street_no}號" if street_no else None

    # estate/phase/building segments
    estate_part = None
    if estate or phase or building:
        estate_part = estate or ""
        if phase:
            estate_part = f"{estate_part} {phase}" if estate_part else phase
        if building:
            estate_part = f"{estate_part} {building}" if estate_part else building

    # label: excludes region / district, format is <street><no號> <estate/phase/building>
    if display_street and no_with_unit:
        street_and_no = f"{display_street}{no_with_unit}"
    else:
        street_and_no = display_street or no_with_unit
    label = " ".join(p for p in [street_and_no, estate_part] if p) or None

    # value:
    # - normal: excludes region / district / street / street_no (= estate_part)
    # - village: excludes region / district (= label, already includes village name + type)
    if is_village:
        value = label
    else:
        value = estate_part

    # full: district + label (old format does not include the region literal)
    full = "".join(p for p in [district, label] if p) or None
    return district, out_street, out_street_no, full, label, value


def _build_en_parts(row, en_region, en_district):
    street_no = _normalize_building_no(row.get("Building_No"))
    estate = _clean(row.get("Estate_Name_Eng"))
    phase = _clean(row.get("Phase_Name_Eng"))
    building = _clean(row.get("Building_Name_Eng"))
    is_village = _is_village(row)

    # Always append the type (VILLAGE / ROAD...); villages do not write to the street field
    display_street = _street_display_name_eng(row)
    if is_village:
        out_street = None
        out_street_no = None
    else:
        out_street = display_street
        out_street_no = street_no

    estate_part = ", ".join(p for p in [building, phase, estate] if p) or None

    if street_no and display_street:
        street_part = f"{street_no} {display_street}"
    else:
        street_part = street_no or display_street

    # label: excludes region / district
    label = ", ".join(p for p in [building, phase, estate, street_part] if p) or None

    # value: village keeps village/no; normal does not
    if is_village:
        value = label
    else:
        value = estate_part

    # full: label + district + region
    full = ", ".join(p for p in [label, en_district, en_region] if p) or None
    return out_street, out_street_no, full, label, value


def _is_placeholder_building_no(value) -> bool:
    """Source number is a placeholder (e.g. *) -> the whole row address is invalid."""
    text = _clean(value)
    return text == "*"


def _is_district_only_addr(
    tc_district, tc_full, en_district, en_full, en_region
) -> bool:
    """Only a district, no street/building details -> invalid address.

    Chinese: tc_district == tc_full_addr (e.g. "北區")
    English: en_full is only district, or district + region
          (e.g. "NORTH DISTRICT, NEW TERRITORIES")
    """
    if not (tc_district and tc_full and tc_district == tc_full):
        return False
    if not en_district or not en_full:
        return True
    if en_full == en_district:
        return True
    if en_region and en_full == f"{en_district}, {en_region}":
        return True
    return False


def _parse_lon_lat(coordinates):
    """Parse 'lon,lat' string -> (lon, lat); return None on failure."""
    text = _clean(coordinates)
    if not text or "," not in text:
        return None
    left, right = text.split(",", 1)
    try:
        lon = float(left.strip())
        lat = float(right.strip())
    except ValueError:
        return None
    return lon, lat


_HTTP_OPENER = None
_PROXY_CONFIG = None


def _get_http_opener():
    """Return a urllib opener, configured with a proxy if enabled in config."""
    global _HTTP_OPENER
    if _HTTP_OPENER is not None:
        return _HTTP_OPENER

    proxy_cfg = _PROXY_CONFIG or {}
    handlers = []
    if proxy_cfg.get("enabled") and proxy_cfg.get("url"):
        handlers.append(
            urllib.request.ProxyHandler(
                {
                    "http": proxy_cfg["url"],
                    "https": proxy_cfg["url"],
                }
            )
        )
    _HTTP_OPENER = urllib.request.build_opener(*handlers)
    return _HTTP_OPENER


def _http_get_json(url: str, timeout: float = 30):
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "adi-address-convertor/1.0",
            "Accept": "application/json",
        },
    )
    opener = _get_http_opener()
    with opener.open(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8")
    return json.loads(raw)


_HK80_TRANSFORMER = None


def _get_hk80_transformer():
    global _HK80_TRANSFORMER
    if Transformer is None:
        return None
    if _HK80_TRANSFORMER is None:
        _HK80_TRANSFORMER = Transformer.from_crs(
            "EPSG:4326",
            "EPSG:2326",
            always_xy=True,
        )
    return _HK80_TRANSFORMER


def wgs84_to_hk80(lon: float, lat: float):
    """WGS84 lon/lat -> HK1980 Grid (easting, northing).

    Prefers pyproj; otherwise falls back to the LandsD transform API.
    """
    transformer = _get_hk80_transformer()
    if transformer is not None:
        easting, northing = transformer.transform(lon, lat)
        return float(easting), float(northing)

    qs = urllib.parse.urlencode(
        {
            "inSys": "wgsgeog",
            "outSys": "hkgrid",
            "lat": f"{lat:.8f}",
            "long": f"{lon:.8f}",
        }
    )
    data = _http_get_json(f"{GEODETIC_TRANSFORM_URL}?{qs}")
    return float(data["hkE"]), float(data["hkN"])


def call_identify_api(easting: float, northing: float, lang: str = "zh"):
    """Call the CSDI Identify API (HK80 easting/northing).

    Docs: https://portal.csdi.gov.hk/csdi-webpage/apidoc/IdentifyAPI
    """
    qs = urllib.parse.urlencode(
        {
            "x": f"{easting:.3f}",
            "y": f"{northing:.3f}",
            "lang": lang,
        }
    )
    payload = _http_get_json(f"{IDENTIFY_API_URL}?{qs}")
    blocks = payload.get("results") or []
    building_only = [
        block
        for block in blocks
        if (block.get("eheader") or "").strip() == "Building Information"
    ]
    payload["results"] = building_only
    return payload


def fetch_identify_for_missing_streets(targets, output_path=None, lang="zh"):
    """Call Identify for addresses with street_no but no street_name, writing results to JSON."""
    output_path = Path(output_path or IDENTIFY_RESULT_JSON)
    results = []
    total = len(targets)
    if total == 0:
        output_path.write_text("[]", encoding="utf-8")
        print("ℹ️ No addresses with a number but no street name need the Identify API.")
        return results

    print(
        f"🛰️ Starting Identify API calls: {total} total (interval {IDENTIFY_REQUEST_INTERVAL_SEC}s)..."
    )
    for i, target in enumerate(targets, start=1):
        entry = {
            **target,
            "hk80": None,
            "identify": None,
            "error": None,
        }
        coords = _parse_lon_lat(target.get("coordinates"))
        if coords is None:
            entry["error"] = "missing_or_invalid_coordinates"
            results.append(entry)
            continue
        lon, lat = coords
        try:
            easting, northing = wgs84_to_hk80(lon, lat)
            entry["hk80"] = {"x": easting, "y": northing}
            entry["identify"] = call_identify_api(easting, northing, lang=lang)
        except (
            urllib.error.URLError,
            urllib.error.HTTPError,
            TimeoutError,
            KeyError,
            ValueError,
            json.JSONDecodeError,
        ) as exc:
            entry["error"] = str(exc)
        results.append(entry)
        if i % 20 == 0 or i == total:
            print(f"   ... Identify progress {i}/{total}")
        if i < total:
            time.sleep(IDENTIFY_REQUEST_INTERVAL_SEC)

    output_path.write_text(
        json.dumps(results, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    ok_n = sum(1 for r in results if r.get("identify") is not None)
    err_n = sum(1 for r in results if r.get("error"))
    print(
        f"✅ Identify results written to {output_path} (success {ok_n}, failed/missing coords {err_n})"
    )
    return results


def _first_identify_address_info(entry):
    identify = entry.get("identify") or {}
    results = identify.get("results") or []
    if not results:
        return None, None
    first_result = results[0]
    infos = first_result.get("addressInfo") or []
    if not infos:
        return first_result, None
    return first_result, infos[0]


def _normalize_street_key(value: str | None) -> str | None:
    """Street name matching key: strip whitespace, unify dashes, uppercase English."""
    text = _clean(value)
    if text is None:
        return None
    text = re.sub(r"[\s\u3000]+", "", text)
    text = text.replace("－", "-").replace("‐", "-").replace("–", "-").replace("—", "-")
    return text.casefold()


_STREET_NAME_INDEX = None


def _load_street_name_index(path=None):
    """Load street_names.json -> {norm_chi/norm_eng: (chi, eng)}."""
    global _STREET_NAME_INDEX
    if _STREET_NAME_INDEX is not None:
        return _STREET_NAME_INDEX

    json_path = Path(path or STREET_NAMES_JSON)
    index = {}
    if not json_path.is_file():
        print(
            f"⚠️ Street name table {json_path} not found; Identify backfill cannot verify street names."
        )
        _STREET_NAME_INDEX = index
        return index

    rows = json.loads(json_path.read_text(encoding="utf-8"))
    for row in rows:
        chi = _clean(row.get("chi_street_name"))
        eng = _clean(row.get("eng_street_name"))
        if chi:
            index[_normalize_street_key(chi)] = (chi, eng)
        if eng:
            index[_normalize_street_key(eng)] = (chi, eng)
    _STREET_NAME_INDEX = index
    print(f"📘 Loaded street name table: {len(rows)} rows (index {len(index)} keys).")
    return index


def _load_official_tc_street_keys(path=None):
    """Official Traditional Chinese street names from street_names.json (normalized key set)."""
    json_path = Path(path or STREET_NAMES_JSON)
    if not json_path.is_file():
        return set()
    keys = set()
    for row in json.loads(json_path.read_text(encoding="utf-8")):
        chi = _clean(row.get("chi_street_name"))
        if chi:
            keys.add(_normalize_street_key(chi))
    return keys


def _load_official_tc_street_names(path=None):
    """Official Traditional Chinese street names from street_names.json (original text)."""
    json_path = Path(path or STREET_NAMES_JSON)
    if not json_path.is_file():
        return []
    names = []
    for row in json.loads(json_path.read_text(encoding="utf-8")):
        chi = _clean(row.get("chi_street_name"))
        if chi:
            names.append(chi)
    return names


# Street/road type suffix -> more likely a real street name
_STREET_TYPE_SUFFIXES = (
    "交匯處",
    "迴旋處",
    "廣場",
    "大道",
    "公路",
    "幹道",
    "環路",
    "小路",
    "村道",
    "村徑",
    "村巷",
    "通道",
    "隧道",
    "街",
    "道",
    "路",
    "里",
    "巷",
    "徑",
    "橋",
    "坊",
    "園",
)
# Common place-name suffix -> more likely a place/village name (not a street)
_PLACE_LIKE_SUFFIXES = (
    "坑",
    "嶺",
    "鄉",
    "塘",
    "頭",
    "尾",
    "地",
    "圍",
    "村",
    "澳",
    "灣",
    "山",
    "田",
    "埔",
    "洲",
    "島",
    "角",
    "塱",
    "滘",
    "磡",
    "涌",
    "壆",
    "輋",
    "窰",
    "窑",
    "排",
    "朗",
    "崗",
    "家",
    "市",
    "仔",
)
_STREET_DIR_CHARS = frozenset("中東西南北")
_STREET_TYPE_AFTER_PLACE = (
    "路",
    "街",
    "道",
    "徑",
    "里",
    "巷",
    "大道",
    "公路",
    "村路",
    "村道",
)


def _classify_unofficial_tc_street(name: str, official_names: list[str]) -> dict:
    """Roughly classify unofficial tc_street_name: street_truncated / likely_street / likely_place.

    Rules (priority order):
    1) Official name = this name prefix + (-segment... or single direction char 中/東/西/南/北)
       -> street_truncated (e.g. 青山公路, 皇后大道)
    2) Ends with a street type suffix -> likely_street (e.g. 荔枝路)
    3) Otherwise -> likely_place (e.g. 下担水坑, 汀角; even if official has 汀角路)
    """
    name = _clean(name) or ""
    nkey = _normalize_street_key(name) or ""
    trunc_matches = []
    place_of_street = []

    for official in official_names:
        okey = _normalize_street_key(official) or ""
        if not okey or okey == nkey or not okey.startswith(nkey):
            continue
        rest = okey[len(nkey) :]
        # Segmented road: 青山公路－荃灣段
        if rest.startswith("-"):
            trunc_matches.append(official)
            continue
        # Directional avenue: 皇后大道中／德輔道西 (rest is exactly one direction char)
        if len(rest) == 1 and rest in _STREET_DIR_CHARS:
            trunc_matches.append(official)
            continue
        # Already a street type, followed by a direction/branch: 漆咸道北
        if any(name.endswith(s) for s in _STREET_TYPE_SUFFIXES):
            if rest and rest[0] in _STREET_DIR_CHARS:
                trunc_matches.append(official)
                continue
        # Place name + road/street: 汀角->汀角路, 大旗嶺->大旗嶺路
        if any(
            rest.startswith(_normalize_street_key(s) or s)
            for s in _STREET_TYPE_AFTER_PLACE
        ):
            place_of_street.append(official)

    if trunc_matches:
        return {
            "class": "street_truncated",
            "matched_official": trunc_matches[:10],
            "matched_official_count": len(trunc_matches),
        }

    if any(name.endswith(s) for s in _STREET_TYPE_SUFFIXES):
        return {
            "class": "likely_street",
            "matched_official": [],
            "matched_official_count": 0,
        }

    return {
        "class": "likely_place",
        "matched_official": place_of_street[:10],
        "matched_official_count": len(place_of_street),
    }


def export_tc_streets_not_in_official_list(
    engine,
    street_names_path=None,
    output_path=None,
    classified_path=None,
):
    """List and export tc_street_name values not present in street_names.json.

    street_truncated (official segmented/directional abbreviations, e.g. 青山公路) is treated as official and excluded.
    The rest are written to the classified JSON:
      - likely_street: has a street type suffix
      - likely_place: more like a place/village name (下担水坑, 汀角)
    """
    official_path = Path(street_names_path or STREET_NAMES_JSON)
    out_path = Path(output_path or UNOFFICIAL_TC_STREETS_JSON)
    class_path = Path(classified_path or UNOFFICIAL_TC_STREETS_CLASSIFIED_JSON)
    print("\n🔎 tc_street_name not in street_names.json list:")

    official_names = _load_official_tc_street_names(official_path)
    official_keys = {
        _normalize_street_key(n) for n in official_names if _normalize_street_key(n)
    }
    if not official_keys:
        print(f"   ⚠️ Could not find or read {official_path.name}")
        return []

    with engine.connect() as conn:
        rows = conn.execute(text("""
            SELECT tc_street_name, COUNT(*) AS cnt
            FROM Address_Flattened
            WHERE tc_street_name IS NOT NULL
              AND TRIM(tc_street_name) != ''
            GROUP BY tc_street_name
            ORDER BY cnt DESC, tc_street_name
        """)).fetchall()

    missing = []
    classified = []
    truncated_n = 0
    truncated_rows = 0
    for name, cnt in rows:
        if _normalize_street_key(name) in official_keys:
            continue
        cls = _classify_unofficial_tc_street(name, official_names)
        # Official segmented/directional abbreviation -> treat as official, do not list as unofficial
        if cls["class"] == "street_truncated":
            truncated_n += 1
            truncated_rows += int(cnt)
            continue
        item = {"tc_street_name": name, "count": int(cnt)}
        missing.append(item)
        classified.append({**item, **cls})

    out_path.write_text(
        json.dumps(missing, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    class_path.write_text(
        json.dumps(classified, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    if truncated_n:
        print(
            f"   ℹ️ Excluded street_truncated (treated as official) "
            f"{truncated_n} types ({truncated_rows} rows)"
        )

    if not missing:
        print("   (all remaining treated as official / no street names to review)")
        print(f"   ✅ Wrote empty list: {out_path}")
        print(f"   ✅ Wrote empty classification: {class_path}")
        return missing

    total_rows = sum(item["count"] for item in missing)
    by_class = {}
    for item in classified:
        by_class.setdefault(item["class"], []).append(item)

    print(
        f"   {len(missing)} street names to review in total"
        f" (total rows {total_rows})"
    )
    for cls_name in ("likely_street", "likely_place"):
        items = by_class.get(cls_name, [])
        rows_n = sum(i["count"] for i in items)
        print(f"   - {cls_name}: {len(items)} types ({rows_n} rows)")
        for item in items[:5]:
            extra = ""
            if item.get("matched_official"):
                extra = f" -> {item['matched_official'][0]}"
            print(f"       · {item['tc_street_name']} ({item['count']}){extra}")
        if len(items) > 5:
            print(f"       ... {len(items) - 5} more types")

    print(f"   ✅ Wrote {out_path}")
    print(f"   ✅ Wrote classification {class_path}")
    return classified


def clear_unofficial_street_fields(engine, unofficial_items):
    """For unofficial street names: clear tc/sc/en street_name and street_no.

    unofficial_items should be the return value of export_tc_streets_not_in_official_list
    (already excludes names in street_names.json and street_truncated).
    """
    names = []
    seen = set()
    for item in unofficial_items or []:
        name = _clean(item.get("tc_street_name"))
        if name and name not in seen:
            seen.add(name)
            names.append(name)

    if not names:
        print("ℹ️ No unofficial street_name/street_no to clear.")
        return 0

    updated = 0
    with engine.begin() as conn:
        for i in range(0, len(names), 400):
            batch = names[i : i + 400]
            params = {f"n{j}": n for j, n in enumerate(batch)}
            placeholders = ", ".join(f":n{j}" for j in range(len(batch)))
            result = conn.execute(
                text(f"""
                    UPDATE Address_Flattened
                    SET
                        tc_street_name = NULL,
                        tc_street_no = NULL,
                        sc_street_name = NULL,
                        sc_street_no = NULL,
                        en_street_name = NULL,
                        en_street_no = NULL
                    WHERE tc_street_name IN ({placeholders})
                """),
                params,
            )
            updated += result.rowcount or 0

    print(
        f"✅ Cleared unofficial street_name/street_no: "
        f"{updated} rows ({len(names)} street names)."
    )
    return updated


def _strip_chi_street_no(caddress: str | None, street_no: str | None) -> str | None:
    """Remove the number from a Chinese address, e.g. 沙頭角公路－龍躍頭段192號 -> 沙頭角公路－龍躍頭段."""
    text = _clean(caddress)
    if text is None:
        return None
    no = _clean(street_no)
    if no:
        for suffix in (f"{no}號", no):
            if text.endswith(suffix):
                return _clean(text[: -len(suffix)])
    m = re.search(r"(.+?)([0-9][0-9A-Za-z]*(?:-[0-9][0-9A-Za-z]*)?)號$", text)
    if m:
        return _clean(m.group(1))
    return text


def _strip_en_street_no(eaddress: str | None, street_no: str | None) -> str | None:
    """Remove the number from an English address, e.g. 192 SHA TAU KOK ROAD - LUNG YEUK TAU -> SHA TAU..."""
    text = _clean(eaddress)
    if text is None:
        return None
    no = _clean(street_no)
    if no:
        prefix = f"{no} "
        if text.upper().startswith(prefix.upper()):
            return _clean(text[len(prefix) :])
        if text.upper() == no.upper():
            return None
    m = re.match(r"^([0-9][0-9A-Za-z]*(?:-[0-9][0-9A-Za-z]*)?)\s+(.+)$", text)
    if m:
        return _clean(m.group(2))
    return text


def _lookup_street_names(caddress, eaddress, street_no, street_index):
    """Verify against street_names.json: on match return (chi, eng), otherwise (None, None).

    Besides exact equality, also supports nested Chinese address suffix matching
    (e.g. 新界粉嶺沙頭角公路－龍躍頭段 -> 沙頭角公路－龍躍頭段).
    """
    chi_cand = _strip_chi_street_no(caddress, street_no)
    eng_cand = _strip_en_street_no(eaddress, street_no)

    for cand in (chi_cand, eng_cand):
        key = _normalize_street_key(cand)
        if key and key in street_index:
            return street_index[key]

    # Chinese: use street name table suffix matching (nested often has 新界/粉嶺 prefix)
    chi_key = _normalize_street_key(chi_cand)
    if chi_key:
        best = None
        best_len = 0
        for key, pair in street_index.items():
            # Only match Chinese keys (containing CJK)
            if not re.search(r"[\u4e00-\u9fff]", key):
                continue
            if chi_key.endswith(key) and len(key) > best_len:
                best = pair
                best_len = len(key)
        if best:
            return best

    return None, None


def _find_nested_facility_addresses(info):
    """Find the first non-empty caddress/eaddress from facility[].addressInfo."""
    facilities = info.get("facility") or []
    if not isinstance(facilities, list):
        return None, None
    for faci in facilities:
        if not isinstance(faci, dict):
            continue
        for nested in faci.get("addressInfo") or []:
            if not isinstance(nested, dict):
                continue
            caddr = _clean(nested.get("caddress"))
            eaddr = _clean(nested.get("eaddress"))
            if caddr or eaddr:
                return caddr, eaddr
    return None, None


def _strip_region_district_suffix_eng(eaddress, en_district=None, en_region=None):
    """Remove the trailing district/region from a nested English address.

    Example: 2 Sha Tau Kok Road - Lung Yeuk Tau, Fanling, New Territories
      -> 2 Sha Tau Kok Road - Lung Yeuk Tau
    """
    text = _clean(eaddress)
    if text is None:
        return None
    parts = [p.strip() for p in text.split(",") if p.strip()]
    drop_keys = {
        "new territories",
        "kowloon",
        "hong kong",
        "fanling",
        "sheung shui",
        "tai po",
        "yuen long",
        "tuen mun",
        "tsuen wan",
        "sai kung",
        "sha tin",
        "tseung kwan o",
    }
    for p in (en_district, en_region):
        if p:
            drop_keys.add(p.casefold())
    while len(parts) > 1 and parts[-1].casefold() in drop_keys:
        parts.pop()
    return _clean(", ".join(parts)) or _clean(eaddress)


def _enrich_identify_addresses(info, entry):
    """Fill in caddress/eaddress: top-level first, otherwise use facility nested."""
    caddress = _clean(info.get("caddress"))
    eaddress = _clean(info.get("eaddress"))
    if caddress and eaddress:
        return caddress, eaddress

    nested_c, nested_e = _find_nested_facility_addresses(info)
    if not caddress and nested_c:
        # Keep the full nested string; street name matching uses suffix matching
        caddress = nested_c
    if not eaddress and nested_e:
        en_region = None
        tc_region = _clean(entry.get("tc_region"))
        for code, name in TC_REGION.items():
            if name == tc_region:
                en_region = EN_REGION.get(code)
                break
        eaddress = _strip_region_district_suffix_eng(
            nested_e,
            _clean(entry.get("en_district")),
            en_region,
        )
    return caddress, eaddress


def _build_identify_update_row(entry, first_info, street_index):
    """Build the Address_Flattened update content based on the street name table match.

    Returns None to indicate no update (avoids overwriting to district-only).
    """
    row_id = entry.get("id")
    ref_csuid = _clean(entry.get("ref_csuid"))
    tc_district = _clean(entry.get("tc_district"))
    en_district = _clean(entry.get("en_district"))
    tc_region = _clean(entry.get("tc_region"))
    en_region = None
    for code, name in TC_REGION.items():
        if name == tc_region:
            en_region = EN_REGION.get(code)
            break

    street_no = _clean(entry.get("tc_street_no")) or _clean(entry.get("en_street_no"))
    cname = _clean(first_info.get("cname"))
    ename = _clean(first_info.get("ename"))
    caddress, eaddress = _enrich_identify_addresses(first_info, entry)

    # No caddress/cname -> do not update (avoid writing district-only)
    if not caddress and not cname and not eaddress and not ename:
        return None

    matched_chi, matched_eng = _lookup_street_names(
        caddress,
        eaddress,
        street_no,
        street_index,
    )
    is_street = bool(matched_chi or matched_eng)

    if is_street:
        # In the street name table -> fill street_name; building uses cname/ename
        tc_street = matched_chi
        en_street = matched_eng
        out_street_no = street_no

        if tc_street and out_street_no:
            tc_street_and_no = f"{tc_street}{out_street_no}號"
        else:
            tc_street_and_no = caddress or (
                f"{out_street_no}號" if out_street_no else tc_street
            )

        if out_street_no and en_street:
            en_street_and_no = f"{out_street_no} {en_street}"
        else:
            en_street_and_no = eaddress or out_street_no or en_street

        tc_value = cname
        en_value = ename
        tc_label = " ".join(p for p in [tc_street_and_no, cname] if p) or None
        en_label = ", ".join(p for p in [ename, en_street_and_no] if p) or None
        tc_full = "".join(p for p in [tc_district, tc_label] if p) or None
        en_full = ", ".join(p for p in [en_label, en_district, en_region] if p) or None
    elif caddress or eaddress:
        # Has an address string but not in the street name table -> not a street, treat the whole string as building
        tc_street = None
        en_street = None
        out_street_no = None

        tc_label = caddress
        en_label = eaddress
        tc_value = caddress
        en_value = eaddress
        tc_full = "".join(p for p in [tc_district, tc_label] if p) or None
        en_full = ", ".join(p for p in [en_label, en_district, en_region] if p) or None
    else:
        # No caddress/eaddress, only cname/ename
        # label/value = building name; keep original street_no; do not fill street_name
        tc_street = None
        en_street = None
        out_street_no = street_no

        tc_label = cname
        en_label = ename
        tc_value = cname
        en_value = ename
        tc_full = "".join(p for p in [tc_district, tc_label] if p) or None
        en_full = ", ".join(p for p in [en_label, en_district, en_region] if p) or None

        # Still district-only -> do not update
        if tc_full == tc_district and (
            en_full in (None, en_district, f"{en_district}, {en_region}")
        ):
            return None

    return {
        "id": row_id,
        "ref_csuid": ref_csuid,
        "tc_street_name": tc_street,
        "tc_street_no": out_street_no,
        "tc_full_addr": tc_full,
        "tc_building_field_label": tc_label,
        "tc_building_field_value": tc_value,
        "sc_street_name": _to_sc(tc_street),
        "sc_street_no": _to_sc(out_street_no),
        "sc_full_addr": _to_sc(tc_full),
        "sc_building_field_label": _to_sc(tc_label),
        "sc_building_field_value": _to_sc(tc_value),
        "en_street_name": en_street,
        "en_street_no": out_street_no,
        "en_full_addr": en_full,
        "en_building_field_label": en_label,
        "en_building_field_value": en_value,
        "_matched_street": is_street,
    }


def apply_identify_json_to_address_table(engine, json_path=None):
    """Backfill Identify JSON into Address_Flattened (same table, same fields).

    Rules:
    1) identify.results is empty -> delete the row
    2) when bdcsuid == ref_csuid:
       - if top-level caddress/eaddress is empty, fill from facility nested
       - verify against street_names.json; on match -> fill street; cname/ename fill building
       - no match but has caddress -> treat as building
       - no caddress but has cname -> label/value=cname, keep street_no
       - neither caddress nor cname -> do not update
    """
    path = Path(json_path or IDENTIFY_RESULT_JSON)
    if not path.is_file():
        print(f"ℹ️ Identify JSON not found, skipping backfill: {path}")
        return

    street_index = _load_street_name_index()
    data = json.loads(path.read_text(encoding="utf-8"))
    delete_rows = []
    update_rows = []
    matched_n = 0
    building_n = 0
    skipped_n = 0

    for entry in data:
        row_id = entry.get("id")
        ref_csuid = _clean(entry.get("ref_csuid"))
        if row_id is None or ref_csuid is None:
            continue

        first_result, first_info = _first_identify_address_info(entry)
        if first_result is None:
            delete_rows.append({"id": row_id, "ref_csuid": ref_csuid})
            continue
        if first_info is None:
            continue

        bdcsuid = _clean(first_info.get("bdcsuid"))
        if bdcsuid != ref_csuid:
            continue

        row = _build_identify_update_row(entry, first_info, street_index)
        if row is None:
            skipped_n += 1
            continue
        if row.pop("_matched_street", False):
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
        f"✅ Identify backfill complete: deleted {len(delete_rows)} rows, "
        f"updated {len(update_rows)} rows"
        f" (street matched {matched_n}, treated as building {building_n}, skipped {skipped_n})."
    )


def clear_street_only_building_labels(engine):
    """If building_field_label is only street_name + street_no and value is empty -> clear label to NULL.

    Chinese example: label = 彌敦道12號, value IS NULL -> label = NULL
    English example: label = 12 NATHAN ROAD, value IS NULL -> label = NULL
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
        f'✅ Cleared "street name + number only" building_field_label: '
        f"tc={tc_n}, sc={sc_n}, en={en_n}"
    )


def transform_to_output_schema(df: pd.DataFrame) -> pd.DataFrame:
    """Convert the MySQL flattened result into the target output columns."""
    rows = []
    identify_targets = []
    skipped_placeholder = 0
    skipped_district_only = 0
    for raw in df.to_dict(orient="records"):
        # A number of "*" would produce *號, not a valid address; skip the whole row
        if _is_placeholder_building_no(raw.get("Building_No")):
            skipped_placeholder += 1
            continue

        region_code = _clean(raw.get("REGION"))
        tc_region = TC_REGION.get(region_code, region_code)
        sc_region = SC_REGION.get(
            region_code, _to_sc(region_code) if region_code else None
        )
        en_region = EN_REGION.get(region_code, region_code)

        tc_district, tc_street, tc_street_no, tc_full, tc_label, tc_value = (
            _build_tc_parts(raw)
        )
        en_district = _clean(raw.get("District_Name_Eng"))
        en_street, en_street_no, en_full, en_label, en_value = _build_en_parts(
            raw, en_region, en_district
        )

        # Has a number but no street name -> call Identify API later (collected even if not written to the main table)
        if not tc_street and tc_street_no:
            identify_targets.append(
                {
                    "id": raw.get("ADDRESS2DID"),
                    "ref_csuid": _clean(raw.get("REFCSUID")),
                    "tc_region": tc_region,
                    "tc_district": tc_district,
                    "tc_street_no": tc_street_no,
                    "tc_full_addr": tc_full,
                    "tc_building_field_label": tc_label,
                    "en_district": en_district,
                    "en_street_no": en_street_no,
                    "en_full_addr": en_full,
                    "coordinates": _clean(raw.get("Coordinates")),
                }
            )

        # Only a district name, no actual address details -> skip
        if _is_district_only_addr(
            tc_district, tc_full, en_district, en_full, en_region
        ):
            skipped_district_only += 1
            continue

        rows.append(
            {
                "id": raw.get("ADDRESS2DID"),
                "ref_csuid": _clean(raw.get("REFCSUID")),
                "tc_region": tc_region,
                "tc_district": tc_district,
                "tc_street_name": tc_street,
                "tc_street_no": tc_street_no,
                "tc_full_addr": tc_full,
                "tc_building_field_label": tc_label,
                "tc_building_field_value": tc_value,
                "sc_region": sc_region,
                "sc_district": _to_sc(tc_district),
                "sc_street_name": _to_sc(tc_street),
                "sc_street_no": _to_sc(tc_street_no),
                "sc_full_addr": _to_sc(tc_full),
                "sc_building_field_label": _to_sc(tc_label),
                "sc_building_field_value": _to_sc(tc_value),
                "en_region": en_region,
                "en_district": en_district,
                "en_street_name": en_street,
                "en_street_no": en_street_no,
                "en_full_addr": en_full,
                "en_building_field_label": en_label,
                "en_building_field_value": en_value,
                "coordinates": _clean(raw.get("Coordinates")),
            }
        )

    if skipped_placeholder:
        print(f'⚠️ Skipped {skipped_placeholder} invalid addresses with number "*".')
    if skipped_district_only:
        print(
            f"⚠️ Skipped {skipped_district_only} rows with only a district and no actual address."
        )
    if identify_targets:
        print(
            f"📌 Found {len(identify_targets)} addresses with a number but no street name; will call the Identify API later."
        )

    out = pd.DataFrame(rows, columns=OUTPUT_COLUMNS)
    out["id"] = pd.to_numeric(out["id"], errors="coerce").astype("Int64")
    out.attrs["identify_targets"] = identify_targets
    # ref_csuid / REFCSUID are text; do not convert to integer

    # Only rows where ALL columns are identical count as duplicates
    exact_dup_mask = out.duplicated(keep=False)
    exact_dups = out.loc[exact_dup_mask].sort_values(by=list(OUTPUT_COLUMNS))
    if not exact_dups.empty:
        group_n = exact_dups.drop_duplicates().shape[0]
        extra_n = len(out) - len(out.drop_duplicates(keep="first"))
        print(
            f"⚠️ Found rows that are completely identical across all columns: "
            f"{group_n} unique contents, will remove {extra_n} extra rows."
        )
        print("—— Duplicate rows (identical across all columns) ——")
        print("—— End ——")
        out = out.drop_duplicates(keep="first")
    else:
        print("✅ No rows that are completely identical across all columns.")

    # Same id but different other columns: not a "duplicate removal" target, but affects PRIMARY KEY
    id_conflict_mask = out.duplicated(subset=["id"], keep=False)
    id_conflicts = out.loc[id_conflict_mask].sort_values(by=["id"])
    if not id_conflicts.empty:
        conflict_ids = id_conflicts["id"].nunique()
        print(
            f"⚠️ {conflict_ids} ids map to multiple rows with different content"
            f" ({len(id_conflicts)} rows total). These will not be removed as duplicates."
        )
        print("—— Same id, different content ——")
        print("—— End ——")
        out.attrs["id_unique"] = False
    else:
        out.attrs["id_unique"] = True

    return out


def space_cjk_for_fts(value):
    """Insert spaces between Chinese characters so unicode61 can index them as single words."""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    text = str(value).strip()
    if not text:
        return None
    spaced = _CJK_RE.sub(r" \1 ", text)
    return re.sub(r"\s+", " ", spaced).strip()


def to_fts_match_query(keyword: str) -> str:
    """Convert a user keyword into FTS5 MATCH syntax.

    Examples:
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
            clauses.append(f"{part}*")
    return " AND ".join(clauses)


def _sqlserver_to_sqlite(sql: str) -> str:
    """Convert SQL Server style [ident] into a SQLite-compatible statement."""
    return re.sub(r"\[([^\]]+)\]", r"\1", sql)


def write_sub_district_map_table(engine):
    """Create sub_district_map and import the accompanying SQL data."""
    if not SUB_DISTRICT_MAP_DDL.is_file():
        raise FileNotFoundError(f"DDL not found: {SUB_DISTRICT_MAP_DDL}")
    if not SUB_DISTRICT_MAP_DATA.is_file():
        raise FileNotFoundError(f"Data file not found: {SUB_DISTRICT_MAP_DATA}")

    create_sql = _sqlserver_to_sqlite(SUB_DISTRICT_MAP_DDL.read_text(encoding="utf-8"))
    # The seq column in the data file is mostly empty string ''; change to NULL to match INT
    data_sql = _sqlserver_to_sqlite(SUB_DISTRICT_MAP_DATA.read_text(encoding="utf-8"))
    data_sql = re.sub(
        r"(VALUES\s*\([^)]*?),\s*''\s*,(\s*'[^']*'\s*\))",
        r"\1, NULL,\2",
        data_sql,
        flags=re.IGNORECASE,
    )

    with engine.begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS sub_district_map"))
        conn.execute(text(create_sql))
        for stmt in data_sql.split(";"):
            stmt = stmt.strip()
            if stmt:
                conn.execute(text(stmt))

    with engine.connect() as conn:
        n = conn.execute(text("SELECT COUNT(*) FROM sub_district_map")).scalar()
    print(f"✅ sub_district_map written with {n} rows.")
    return n


def write_address_table(engine, df: pd.DataFrame):
    """Write Address_Flattened; set PRIMARY KEY when id is unique."""
    id_unique = bool(df.attrs.get("id_unique", df["id"].is_unique))
    id_col_sql = "id INTEGER PRIMARY KEY" if id_unique else "id INTEGER"
    if not id_unique:
        print(
            "⚠️ id is not unique; PRIMARY KEY not set when creating the table to avoid write failure."
        )

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
        conn.execute(text("DROP TABLE IF EXISTS Address_FTS"))
        conn.execute(text("DROP TABLE IF EXISTS Address_Flattened"))
        conn.execute(text(create_sql))
    df.to_sql("Address_Flattened", engine, index=False, if_exists="append")


def create_address_indexes(engine):
    """Create indexes based on the existing Java query paths (ignoring space, prioritizing read performance)."""
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
    """Create the Address_FTS (FTS5) virtual table."""
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS Address_FTS"))
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
        src["id"] = pd.to_numeric(src["id"], errors="coerce").astype("Int64")
        for col in _FTS_TEXT_COLS:
            src[col] = src[col].map(space_cjk_for_fts)

        cols = ["id", *_FTS_TEXT_COLS]
        placeholders = ", ".join(f":{c}" for c in cols)
        insert_sql = text(
            f"INSERT INTO Address_FTS ({', '.join(cols)}) VALUES ({placeholders})"
        )
        rows = src.where(pd.notnull(src), None).to_dict(orient="records")
        for i in range(0, len(rows), 1000):
            conn.execute(insert_sql, rows[i : i + 1000])


def verify_sqlite_db(engine, expect_rows=None):
    """Verify the generated SQLite: tables, columns, row counts, Chinese segmentation, FTS MATCH, JOIN."""
    db_path = engine.url.database
    print("\n🧪 Starting SQLite database verification...")
    print(f"   File path: {db_path}")

    results = []

    def check(name, ok, detail=""):
        status = "PASS" if ok else "FAIL"
        results.append(ok)
        suffix = f" — {detail}" if detail else ""
        print(f"   [{status}] {name}{suffix}")

    with engine.connect() as conn:
        tables = {
            row[0]
            for row in conn.execute(
                text("SELECT name FROM sqlite_master WHERE type IN ('table', 'view')")
            )
        }
        check("Address_Flattened exists", "Address_Flattened" in tables)
        check("Address_FTS exists", "Address_FTS" in tables)

        cols = [
            row[1] for row in conn.execute(text("PRAGMA table_info(Address_Flattened)"))
        ]
        missing = [c for c in OUTPUT_COLUMNS if c not in cols]
        check(
            "Main table columns complete",
            not missing,
            f"missing={missing}" if missing else "ok",
        )

        flat_n = conn.execute(text("SELECT COUNT(*) FROM Address_Flattened")).scalar()
        fts_n = conn.execute(text("SELECT COUNT(*) FROM Address_FTS")).scalar()
        check("Main table row count > 0", flat_n > 0, f"Address_Flattened={flat_n}")
        check("FTS row count matches main table", flat_n == fts_n, f"FTS={fts_n}")
        if expect_rows is not None:
            check(
                "Row count matches this import",
                flat_n == expect_rows,
                f"expect={expect_rows}",
            )

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
                row
                for row in sample
                if re.search(r"[\u4e00-\u9fff]{2,}", row["flat_addr"] or "")
            ),
            None,
        )

        if sample is None:
            check("Sample Chinese address", False, "no sample Chinese address found")
        else:
            flat_addr = sample["flat_addr"] or ""
            fts_addr = sample["fts_addr"] or ""
            flat_has_cjk_run = bool(re.search(r"[\u4e00-\u9fff]{2,}", flat_addr))
            fts_spaced = sample["fts_space_pos"] > 0 and bool(
                re.search(r"[\u4e00-\u9fff] [\u4e00-\u9fff]", fts_addr)
            )
            check(
                "Main table keeps original text (with consecutive Chinese)",
                flat_has_cjk_run and flat_addr != fts_addr,
                f'id={sample["id"]} flat={flat_addr!r}',
            )
            check(
                "FTS has Chinese segmented",
                fts_spaced and sample["fts_len"] > sample["flat_len"],
                f'fts={fts_addr!r} len {sample["flat_len"]}→{sample["fts_len"]}',
            )
            check(
                "REGION mapping is correct",
                sample["tc_region"] in TC_REGION.values()
                and sample["sc_region"] in SC_REGION.values()
                and sample["en_region"] in EN_REGION.values(),
                f'tc={sample["tc_region"]} sc={sample["sc_region"]} en={sample["en_region"]}',
            )

        phrase_q = to_fts_match_query("旺角")
        match_phrase = conn.execute(
            text("SELECT COUNT(*) FROM Address_FTS WHERE Address_FTS MATCH :q"),
            {"q": phrase_q},
        ).scalar()
        match_raw = conn.execute(
            text("SELECT COUNT(*) FROM Address_FTS WHERE Address_FTS MATCH '旺角'")
        ).scalar()
        check(
            f"FTS MATCH {phrase_q!r} has results",
            match_phrase > 0,
            f"hits={match_phrase}",
        )
        check(
            "FTS MATCH '旺角' should be near 0 (unsegmented syntax)",
            match_raw == 0,
            f"hits={match_raw}",
        )

        join_n = conn.execute(
            text("""
            SELECT COUNT(*)
            FROM Address_FTS f
            JOIN Address_Flattened a ON a.id = f.id
            WHERE Address_FTS MATCH :q
        """),
            {"q": phrase_q},
        ).scalar()
        orphan_n = conn.execute(
            text("""
            SELECT COUNT(*)
            FROM Address_FTS f
            WHERE Address_FTS MATCH :q
              AND NOT EXISTS (
                  SELECT 1 FROM Address_Flattened a WHERE a.id = f.id
              )
        """),
            {"q": phrase_q},
        ).scalar()
        check(
            "FTS JOIN main table succeeded",
            join_n > 0 and orphan_n == 0,
            f"join_hits={join_n}, orphans={orphan_n}",
        )

        conn.execute(text("DROP TABLE IF EXISTS _verify_fts_vocab"))
        conn.execute(
            text(
                "CREATE VIRTUAL TABLE _verify_fts_vocab USING fts5vocab(Address_FTS, 'row')"
            )
        )
        vocab = dict(conn.execute(text("""
                SELECT term, cnt FROM _verify_fts_vocab
                WHERE term IN ('旺', '角', '旺角')
            """)).fetchall())
        conn.execute(text("DROP TABLE IF EXISTS _verify_fts_vocab"))
        check(
            "Vocabulary contains single char '旺'",
            vocab.get("旺", 0) > 0,
            f"cnt={vocab.get('旺', 0)}",
        )
        check(
            "Vocabulary contains single char '角'",
            vocab.get("角", 0) > 0,
            f"cnt={vocab.get('角', 0)}",
        )
        check(
            "Vocabulary does not contain '旺角'",
            vocab.get("旺角", 0) == 0,
            f"cnt={vocab.get('旺角', 0)}",
        )

    passed = sum(1 for ok in results if ok)
    total = len(results)
    all_ok = all(results)
    print(f"\n🧪 Verification finished: {passed}/{total} passed")
    if all_ok:
        print("✅ SQLite database verification passed.")
    else:
        print(
            "❌ SQLite database verification failed; please check the FAIL items above."
        )
    return all_ok


def run_sync_and_verify(mysql_engine, skip_identify_api: bool = False):
    print(
        "🔄 Connecting to MySQL and running the complex query; this may take a few minutes (depends on data volume)..."
    )
    raw_df = pd.read_sql(sql_query, mysql_engine)
    print(f"✅ Successfully read {len(raw_df)} address records from MySQL!")

    print("🧩 Converting to tc/sc/en output structure...")
    df = transform_to_output_schema(raw_df)
    print(f"✅ Conversion complete, ready to write {len(df)} records.")

    print("💾 Writing data to SQLite (adi_address.sqlite)...")
    write_address_table(sqlite_engine, df)

    print("⚙️ Creating Address_Flattened indexes...")
    create_address_indexes(sqlite_engine)

    print("🗺️ Creating sub_district_map and importing data...")
    write_sub_district_map_table(sqlite_engine)

    # Has street_no but no street_name -> Identify API, results written to a separate JSON
    identify_targets = df.attrs.get("identify_targets") or []
    if skip_identify_api:
        if IDENTIFY_RESULT_JSON.is_file():
            print(
                f"⏭️ Skipping Identify API as requested; reusing existing {IDENTIFY_RESULT_JSON.name}"
            )
        else:
            print(
                f"⚠️ Skipping Identify API as requested, but {IDENTIFY_RESULT_JSON.name} was not found; "
                f"backfill will be skipped."
            )
    else:
        fetch_identify_for_missing_streets(identify_targets, IDENTIFY_RESULT_JSON)

    apply_identify_json_to_address_table(sqlite_engine, IDENTIFY_RESULT_JSON)
    clear_street_only_building_labels(sqlite_engine)

    # Non-official street names: export the list first, then clear street_name/street_no (except street_truncated)
    unofficial = export_tc_streets_not_in_official_list(sqlite_engine)
    clear_unofficial_street_fields(sqlite_engine, unofficial)

    print("🔍 Creating FTS5 virtual table Address_FTS...")
    build_fts5_index(sqlite_engine)

    example_q = to_fts_match_query("旺角")
    print("🎉 Migration completed successfully! Your SQLite database is ready.")
    print("   - Address_Flattened : tc/sc/en address output table")
    print(
        "   - Address_FTS       : FTS5 full-text search virtual table (Chinese segmented)"
    )
    print("   - sub_district_map  : district/sub-district mapping table")
    print(f"   - Identify JSON    : {IDENTIFY_RESULT_JSON.name}")
    print(f"   - Non-official street list : {UNOFFICIAL_TC_STREETS_JSON.name}")
    print(
        f"   - Non-official street classification : {UNOFFICIAL_TC_STREETS_CLASSIFIED_JSON.name}"
    )
    print("   Example query:")
    print("   SELECT a.* FROM Address_FTS f")
    print("   JOIN Address_Flattened a ON a.id = f.id")
    print(f"   WHERE Address_FTS MATCH '{example_q}';")
    verified = verify_sqlite_db(sqlite_engine, expect_rows=len(df))

    print("\n🔎 tc_street_name containing spaces:")
    with sqlite_engine.connect() as conn:
        rows = conn.execute(text("""
            SELECT DISTINCT tc_street_name
            FROM Address_Flattened
            WHERE tc_street_name IS NOT NULL
              AND tc_street_name LIKE '% %'
            ORDER BY tc_street_name
        """)).fetchall()
    if not rows:
        print("   (no street names with spaces)")
    else:
        for row in rows:
            print(f"   - {row[0]}")

    print_strange_street_nos(sqlite_engine)

    return verified


def _parse_args(argv=None):
    """Parse command-line arguments for MySQL config and runtime options."""
    import argparse

    parser = argparse.ArgumentParser(
        description="Migrate MySQL address data to SQLite with tc/sc/en output."
    )
    parser.add_argument(
        "--config", type=Path, default=CONFIG_PATH, help="Path to config JSON file."
    )
    parser.add_argument("--mysql-user", default=None, help="MySQL username")
    parser.add_argument("--mysql-pass", default=None, help="MySQL password")
    parser.add_argument("--mysql-host", default=None, help="MySQL host")
    parser.add_argument("--mysql-port", type=int, default=None, help="MySQL port")
    parser.add_argument("--mysql-db", default=None, help="MySQL database name")
    parser.add_argument(
        "--sqlite-path", default=None, help="Output SQLite database file path"
    )
    parser.add_argument(
        "--proxy-url", default=None,
        help="Proxy URL for API calls, e.g. http://user:pass@host:port "
             "(enables proxy; overrides config.json)",
    )
    parser.add_argument(
        "--skip-identify-api",
        "--skip-identify",
        action="store_true",
        help="Skip calling the Identify API",
    )
    parser.add_argument(
        "--verify",
        "--verify-only",
        action="store_true",
        help="Only verify the existing SQLite database",
    )
    return parser.parse_args(argv)


if __name__ == "__main__":
    args = _parse_args()
    config = load_config(Path(args.config))

    # Apply CLI overrides
    if args.mysql_user:
        config["mysql"]["user"] = args.mysql_user
    if args.mysql_pass:
        config["mysql"]["password"] = args.mysql_pass
    if args.mysql_host:
        config["mysql"]["host"] = args.mysql_host
    if args.mysql_port:
        config["mysql"]["port"] = args.mysql_port
    if args.mysql_db:
        config["mysql"]["database"] = args.mysql_db
    if args.sqlite_path:
        config["sqlite"]["path"] = args.sqlite_path
    if args.proxy_url:
        config["proxy"]["enabled"] = True
        config["proxy"]["url"] = args.proxy_url

    # Configure the HTTP opener (used by API calls) with proxy settings
    _PROXY_CONFIG = config.get("proxy") or {}

    sqlite_engine = build_sqlite_engine(config)

    if args.verify:
        raise SystemExit(0 if verify_sqlite_db(sqlite_engine) else 1)

    mysql_engine = build_mysql_engine(config)

    if args.skip_identify_api:
        print("ℹ️ Option: --skip-identify-api (do not call the Identify API)")

    raise SystemExit(
        0
        if run_sync_and_verify(mysql_engine, skip_identify_api=args.skip_identify_api)
        else 1
    )
