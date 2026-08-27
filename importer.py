"""
엑셀 파일(원본 워크북 형식 또는 쿠팡 다운로드 파일)을 읽어
DB로 적재하는 함수 모음.
"""
import pandas as pd
from db import get_conn, commit as db_commit


def _to_bool(v):
    if isinstance(v, str):
        return v.strip() in ("O", "o", "TRUE", "True", "1", "예")
    return bool(v)


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

    conn = get_conn()
    cur = conn.cursor()
    count = 0
    for _, row in df.iterrows():
        if pd.isna(row.get("option_code")):
            continue
        cur.execute("""
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
        """, (
            str(row.get("product_code") or ""),
            str(row.get("option_code")),
            row.get("sample_code"),
            int(row["set_qty"]) if not pd.isna(row.get("set_qty")) else 1,
            row.get("product_name"),
            row.get("option_name"),
            row.get("image_url"),
            row.get("supplier_1688_url"),
            row.get("supplier_name_cn"),
            row.get("product_name_cn"),
            row.get("option_name_cn"),
            float(row["price_cny"]) if not pd.isna(row.get("price_cny")) else None,
            row.get("size_tag"),
            _to_bool(row.get("strategic")),
            _to_bool(row.get("discontinued")),
            row.get("memo"),
        ))
        count += 1
    db_commit(conn)
    conn.close()
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
