"""
엑셀 파일(원본 워크북 형식 또는 쿠팡 다운로드 파일)을 읽어
DB로 적재하는 함수 모음.
"""
import time
import pandas as pd
from db import get_conn, commit as db_commit


def _execute_batch_with_retry(sql, batch, retries=2, delay=1.0):
    """배치를 새 연결로 실행하고 즉시 커밋한다.
    Turso(원격 DB)는 가끔 일시적인 연결 끊김("stream not found" 등)이 발생할 수 있는데,
    끊긴 연결을 그대로 재사용하면 다시 실패하므로, 실패 시 매번 완전히 새 연결을 맺어 재시도한다."""
    last_err = None
    for attempt in range(retries + 1):
        conn = get_conn()
        try:
            cur = conn.cursor()
            cur.executemany(sql, batch)
            db_commit(conn)
            conn.close()
            return
        except Exception as e:
            last_err = e
            try:
                conn.close()
            except Exception:
                pass
            if attempt < retries:
                time.sleep(delay)
    raise last_err


def _to_bool(v):
    if isinstance(v, str):
        return v.strip() in ("O", "o", "TRUE", "True", "1", "예")
    return bool(v)


def _clean_text(v):
    """빈 칸(NaN)은 None(=SQL NULL)으로, 값이 있으면 문자열로 변환.
    Turso(원격 DB)는 텍스트 컬럼에 NaN 실수값이 그대로 들어가면 거부하므로 반드시 정리한다."""
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return None
    return str(v)


def _clean_num(v, default=0):
    """빈 칸(NaN)은 기본값(0)으로 치환. Turso는 숫자(REAL/INTEGER) 컬럼에 NULL 대신
    빈 값이 들어오면 타입 에러(JSON parse error: invalid type: null, expected f64)를 낸다."""
    if v is None or pd.isna(v):
        return default
    return v


def import_product_master(file, sheet_name=0):
    """
    '단일상품' 시트 형식의 엑셀을 읽어 products 테이블에 upsert.
    기대 컬럼(헤더가 1행): 상품코드, 옵션코드, 샘플코드, 구성개수, 상품명, 옵션명,
    이미지주소(발주), 1688주소, 업체명, 제품명(중문), 옵션명(중문), 가격(위안화),
    사이즈, 전략상품, 단종, 발주메모
    """
    df = pd.read_excel(file, sheet_name=sheet_name, header=0)
    df = df.rename(columns={
        "상품코드": "product_code",
        "옵션코드": "option_code",
        "샘플코드": "sample_code",
        "구성개수": "set_qty",
        "상품명": "product_name",
        "옵션명": "option_name",
        "이미지주소(발주)": "image_url",
        "1688주소": "supplier_1688_url",
        "업체명": "supplier_name_cn",
        "제품명(중문)": "product_name_cn",
        "옵션명(중문)": "option_name_cn",
        "가격(위안화)": "price_cny",
        "사이즈": "size_tag",
        "전략상품": "strategic",
        "단종": "discontinued",
        "발주메모": "memo",
    })

    required = ["product_code", "option_code"]
    for col in required:
        if col not in df.columns:
            raise ValueError(f"필수 컬럼 누락: {col}")

    insert_sql = """
        INSERT INTO products (
            product_code, option_code, sample_code, set_qty, product_name,
            option_name, image_url, supplier_1688_url, supplier_name_cn,
            product_name_cn, option_name_cn, price_cny, size_tag,
            strategic, discontinued, memo
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(option_code) DO UPDATE SET
            product_code=excluded.product_code,
            sample_code=excluded.sample_code,
            set_qty=excluded.set_qty,
            product_name=excluded.product_name,
            option_name=excluded.option_name,
            image_url=excluded.image_url,
            supplier_1688_url=excluded.supplier_1688_url,
            supplier_name_cn=excluded.supplier_name_cn,
            product_name_cn=excluded.product_name_cn,
            option_name_cn=excluded.option_name_cn,
            price_cny=excluded.price_cny,
            size_tag=excluded.size_tag,
            strategic=excluded.strategic,
            discontinued=excluded.discontinued,
            memo=excluded.memo
    """

    # 먼저 전체 행을 정리해서 파라미터 리스트로 모은다 (행마다 네트워크 왕복하지 않기 위함).
    params_list = []
    for _, row in df.iterrows():
        if pd.isna(row.get("option_code")):
            continue
        params_list.append((
            str(row.get("product_code") or ""),
            str(row.get("option_code")),
            _clean_text(row.get("sample_code")),
            int(_clean_num(row.get("set_qty"), 1)),
            _clean_text(row.get("product_name")),
            _clean_text(row.get("option_name")),
            _clean_text(row.get("image_url")),
            _clean_text(row.get("supplier_1688_url")),
            _clean_text(row.get("supplier_name_cn")),
            _clean_text(row.get("product_name_cn")),
            _clean_text(row.get("option_name_cn")),
            float(_clean_num(row.get("price_cny"), 0)),
            _clean_text(row.get("size_tag")),
            _to_bool(row.get("strategic")),
            _to_bool(row.get("discontinued")),
            _clean_text(row.get("memo")),
        ))

    # 100건씩 나눠서 배치 전송한다. 각 배치는 독립적으로 커밋되고,
    # 중간에 연결이 끊기면(예: "stream not found") 새 연결로 자동 재시도한다.
    # ON CONFLICT ... DO UPDATE(upsert)라서 재시도해도 중복 없이 안전하다.
    BATCH_SIZE = 100
    for i in range(0, len(params_list), BATCH_SIZE):
        batch = params_list[i:i + BATCH_SIZE]
        _execute_batch_with_retry(insert_sql, batch)

    count = len(params_list)
    return count


def import_coupang_stock(file, sheet_name=0):
    """
    쿠팡에서 다운로드한 '쿠팡업로드상품' 형식 엑셀을 읽어
    coupang_stock_snapshot 테이블을 통째로 갱신(원본 매크로처럼 매번 대체).
    기대 컬럼: 옵션코드, 쿠팡판매가격, 이우입고중재고, 판매가능재고, 입고예정재고,
    이번달 보관료, 고객반품, 7일매출, 7일판매수, 30일매출, 30일판매수
    """
    df = pd.read_excel(file, sheet_name=sheet_name, header=0)
    df = df.rename(columns={
        "옵션코드": "option_code",
        "쿠팡판매가격": "coupang_price",
        "이우입고중재고": "stock_incoming_ewoo",
        "판매가능재고": "stock_sellable",
        "입고예정재고": "stock_incoming_coupang",
        "이번달 누적 보관료": "storage_fee_month",
        "고객반품": "returns_qty",
        "7일매출": "sales_amt_7d",
        "7일판매수": "sales_qty_7d",
        "30일매출": "sales_amt_30d",
        "30일판매수": "sales_qty_30d",
    })

    if "option_code" not in df.columns:
        raise ValueError("필수 컬럼 누락: 옵션코드")

    conn = get_conn()
    cur = conn.cursor()
    # 원본 매크로(UpdateCoupangStockQuietly)와 동일하게 스냅샷 전체 교체
    cur.execute("DELETE FROM coupang_stock_snapshot")
    count = 0
    for _, row in df.iterrows():
        if pd.isna(row.get("option_code")):
            continue

        def num(col):
            v = row.get(col)
            return float(v) if not pd.isna(v) else 0

        cur.execute("""
            INSERT INTO coupang_stock_snapshot (
                option_code, coupang_price, stock_incoming_ewoo, stock_sellable,
                stock_incoming_coupang, storage_fee_month, returns_qty,
                sales_amt_7d, sales_qty_7d, sales_amt_30d, sales_qty_30d
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
        """, (
            str(row["option_code"]),
            num("coupang_price"), num("stock_incoming_ewoo"), num("stock_sellable"),
            num("stock_incoming_coupang"), num("storage_fee_month"), num("returns_qty"),
            num("sales_amt_7d"), num("sales_qty_7d"), num("sales_amt_30d"), num("sales_qty_30d"),
        ))
        count += 1
    db_commit(conn)
    conn.close()
    return count


def import_keydari_incoming(file, sheet_name=0):
    """'키다리쿠팡입고' 형식 엑셀 → keydari_incoming_snapshot 갱신."""
    df = pd.read_excel(file, sheet_name=sheet_name, header=0)
    df = df.rename(columns={"옵션코드": "option_code", "입고수량": "incoming_qty"})
    if "option_code" not in df.columns:
        raise ValueError("필수 컬럼 누락: 옵션코드")

    conn = get_conn()
    cur = conn.cursor()
    cur.execute("DELETE FROM keydari_incoming_snapshot")
    count = 0
    for _, row in df.iterrows():
        if pd.isna(row.get("option_code")):
            continue
        qty = row.get("incoming_qty")
        qty = int(qty) if not pd.isna(qty) else 0
        cur.execute(
            "INSERT INTO keydari_incoming_snapshot (option_code, incoming_qty) VALUES (?,?)",
            (str(row["option_code"]), qty),
        )
        count += 1
    db_commit(conn)
    conn.close()
    return count
