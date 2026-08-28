"""
로켓그로스 발주/입고 관리 - 데이터베이스 레이어

두 가지 모드로 동작한다:
1) 로컬 개발용: 그냥 로컬 SQLite 파일 (기본값, 아무 환경변수 없이 실행하면 이 모드)
2) 배포용(Turso): 환경변수 TURSO_DATABASE_URL / TURSO_AUTH_TOKEN 이 설정되어 있으면
   Turso(libSQL, 무료 클라우드 DB)에 매 요청마다 직접 연결한다.
   (참고: 로컬 파일에 캐시해뒀다가 동기화하는 "임베디드 복제본" 방식은 한동안 안 쓰다가
   다시 쓰면 "stream not found" 에러가 나는 등 아직 불안정해서, 대신 매번 원격에 바로
   붙는 단순한 방식을 쓴다. 이 앱 정도 트래픽에서는 속도 차이가 거의 없다.)
"""
import os
import sqlite3
from pathlib import Path

TURSO_URL = os.environ.get("TURSO_DATABASE_URL", "")
TURSO_TOKEN = os.environ.get("TURSO_AUTH_TOKEN", "")
USE_TURSO = bool(TURSO_URL and TURSO_TOKEN)

LOCAL_DB_PATH = Path(__file__).parent / "rocket_growth.db"

if USE_TURSO:
    import libsql_experimental as libsql


def get_conn():
    """
    Turso 모드: 로컬 캐시 없이 매번 원격 DB에 직접 연결한다.
    로컬 모드: 평범한 sqlite3 연결.
    두 경우 모두 '?' 플레이스홀더의 표준 DBAPI2 스타일로 사용 가능.
    """
    if USE_TURSO:
        conn = libsql.connect(TURSO_URL, auth_token=TURSO_TOKEN)
        return conn
    conn = sqlite3.connect(LOCAL_DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def commit(conn):
    """원격 직접 연결 모드에서는 conn.commit()만으로 충분하다 (별도 동기화 불필요)."""
    conn.commit()


def fetchall_dict(cur):
    """libsql은 sqlite3.Row 같은 dict-style 접근을 지원하지 않으므로
    cursor.description을 이용해 dict 리스트로 통일해서 반환한다."""
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def fetchone_dict(cur):
    cols = [d[0] for d in cur.description]
    row = cur.fetchone()
    return dict(zip(cols, row)) if row else None



def init_db():
    conn = get_conn()
    cur = conn.cursor()

    # 상품 마스터 (원본 '단일상품' 시트 대응)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS products (
        option_code TEXT PRIMARY KEY,       -- 옵션코드 (예: SM0001-1) - 실제 유일 식별자
        product_code TEXT,                  -- 상품코드 (예: SM0001) - 여러 옵션이 공유 가능
        sample_code TEXT,                   -- 샘플코드
        set_qty INTEGER DEFAULT 1,          -- 구성개수
        product_name TEXT,                  -- 상품명
        option_name TEXT,                   -- 옵션명
        image_url TEXT,                     -- 이미지주소(발주)
        supplier_1688_url TEXT,             -- 1688주소
        supplier_name_cn TEXT,              -- 업체명(중문)
        product_name_cn TEXT,               -- 제품명(중문)
        option_name_cn TEXT,                -- 옵션명(중문)
        price_cny REAL,                     -- 가격(위안화)
        size_tag TEXT,                      -- 사이즈 (극소/소/중/대 등)
        strategic BOOLEAN DEFAULT 0,        -- 전략상품 여부
        discontinued BOOLEAN DEFAULT 0,     -- 단종 여부
        registration_status TEXT DEFAULT '등록대기', -- 등록대기 / 등록완료
        memo TEXT,                          -- 발주메모
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    )""")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_products_product_code ON products(product_code)")

    # 이미 만들어진 DB에는 registration_status 컬럼이 없을 수 있으므로 안전하게 추가 시도
    try:
        cur.execute("ALTER TABLE products ADD COLUMN registration_status TEXT DEFAULT '등록대기'")
    except Exception:
        pass  # 이미 컬럼이 있으면 에러 무시

    # 쿠팡 재고/판매 스냅샷 (원본 '쿠팡업로드상품' 시트 대응, 쿠팡 다운로드 파일로 매번 갱신)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS coupang_stock_snapshot (
        option_code TEXT PRIMARY KEY,
        coupang_price REAL,                 -- 쿠팡판매가격
        stock_incoming_ewoo INTEGER DEFAULT 0,  -- 이우입고중재고
        stock_sellable INTEGER DEFAULT 0,       -- 판매가능재고 (현재재고)
        stock_incoming_coupang INTEGER DEFAULT 0, -- 입고예정재고
        storage_fee_month REAL DEFAULT 0,
        returns_qty INTEGER DEFAULT 0,
        sales_amt_7d REAL DEFAULT 0,
        sales_qty_7d INTEGER DEFAULT 0,
        sales_amt_30d REAL DEFAULT 0,
        sales_qty_30d INTEGER DEFAULT 0,
        updated_at TEXT DEFAULT CURRENT_TIMESTAMP
    )""")

    # 키다리(2번 창고) 입고예정 스냅샷 (원본 '키다리쿠팡입고' 시트 대응)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS keydari_incoming_snapshot (
        option_code TEXT PRIMARY KEY,
        incoming_qty INTEGER DEFAULT 0,
        updated_at TEXT DEFAULT CURRENT_TIMESTAMP
    )""")

    # 거래처 / 입고창고 (원본 '사용자정보' 시트 대응)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS warehouses (
        warehouse_id INTEGER PRIMARY KEY AUTOINCREMENT,
        category TEXT,          -- 구분 (샘플/쿠팡 등)
        warehouse_code TEXT,    -- 입고창고 코드
        warehouse_name TEXT,    -- 창고이름
        phone TEXT,
        address TEXT,
        zipcode TEXT
    )""")

    # 발주 회차 (원본 '발주기록' 시트 대응)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS order_batches (
        batch_id INTEGER PRIMARY KEY AUTOINCREMENT,
        batch_date TEXT NOT NULL,
        batch_no INTEGER NOT NULL,
        memo TEXT,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    )""")

    # 발주 상세 (원본 '발주상품기록' + '재발주' 시트 대응)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS order_items (
        order_item_id INTEGER PRIMARY KEY AUTOINCREMENT,
        batch_id INTEGER REFERENCES order_batches(batch_id),
        option_code TEXT NOT NULL,
        warehouse_code TEXT,        -- 입고창고
        delivery_type TEXT,         -- 입고구분 (쿠팡/키다리 등)
        delivery_method TEXT,       -- 입고방법 (택배/화물 등)
        order_qty INTEGER NOT NULL,
        unit_price_cny REAL,
        order_amount REAL,          -- 발주금액 (수량*단가*환율 등)
        memo TEXT,
        status TEXT DEFAULT '발주대기',   -- 발주대기 / 발주완료 / 입고완료
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    )""")

    # 입고 기록 (실제 물건이 들어온 시점 기록)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS receiving_records (
        receiving_id INTEGER PRIMARY KEY AUTOINCREMENT,
        order_item_id INTEGER REFERENCES order_items(order_item_id),
        received_qty INTEGER NOT NULL,
        received_date TEXT NOT NULL,
        box_no TEXT,
        memo TEXT,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    )""")

    commit(conn)
    conn.close()


def next_batch_no(batch_date: str) -> int:
    """해당 날짜의 다음 발주회차 번호."""
    conn = get_conn()
    cur = conn.execute(
        "SELECT MAX(batch_no) as m FROM order_batches WHERE batch_date = ?",
        (batch_date,),
    )
    row = fetchone_dict(cur)
    conn.close()
    return ((row["m"] if row else None) or 0) + 1
