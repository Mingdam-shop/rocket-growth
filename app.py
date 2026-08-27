import streamlit as st
import pandas as pd
from datetime import date
import io
import os

from db import init_db, get_conn, next_batch_no, commit as db_commit
import importer

st.set_page_config(page_title="로켓그로스 발주/입고 관리", layout="wide")
init_db()

# ---------------------------------------------------------------------------
# 간단한 팀 공유 비밀번호 로그인
# 배포 환경(Railway/Render 등)의 환경변수 APP_PASSWORD 에 비밀번호를 설정하세요.
# 설정하지 않으면(로컬 테스트용) 로그인 없이 바로 진입합니다.
# ---------------------------------------------------------------------------
APP_PASSWORD = os.environ.get("APP_PASSWORD", "")

if APP_PASSWORD:
    if "authed" not in st.session_state:
        st.session_state.authed = False

    if not st.session_state.authed:
        st.title("🔒 로켓그로스 관리 프로그램")
        pw = st.text_input("팀 비밀번호를 입력하세요", type="password")
        if st.button("입장"):
            if pw == APP_PASSWORD:
                st.session_state.authed = True
                st.rerun()
            else:
                st.error("비밀번호가 틀렸습니다.")
        st.stop()

# ---------------------------------------------------------------------------
# 공통 데이터 로더
# ---------------------------------------------------------------------------

def load_reorder_view() -> pd.DataFrame:
    """
    원본 '재발주' 시트의 XLOOKUP 로직을 재현:
    products + coupang_stock_snapshot + keydari_incoming_snapshot 을 옵션코드로 조인.
    """
    conn = get_conn()
    df = pd.read_sql_query("""
        SELECT
            p.option_code,
            p.product_code,
            p.product_name,
            p.option_name,
            p.size_tag,
            p.image_url,
            p.supplier_1688_url,
            p.price_cny,
            p.set_qty,
            p.strategic,
            p.discontinued,
            COALESCE(s.stock_sellable, 0)          AS current_stock,
            COALESCE(s.stock_incoming_coupang, 0)   AS coupang_incoming,
            COALESCE(k.incoming_qty, 0)             AS keydari_incoming,
            COALESCE(s.sales_qty_7d, 0)             AS sales_7d,
            COALESCE(s.sales_qty_30d, 0)            AS sales_30d,
            COALESCE(s.coupang_price, 0)            AS coupang_price
        FROM products p
        LEFT JOIN coupang_stock_snapshot s ON s.option_code = p.option_code
        LEFT JOIN keydari_incoming_snapshot k ON k.option_code = p.option_code
        WHERE p.discontinued = 0
        ORDER BY p.product_code, p.option_code
    """, conn)
    conn.close()

    if df.empty:
        return df

    # 기준판매수: 최근 30일 판매량을 하루 평균으로 환산 (원본 T열 '기준판매수' 근사)
    df["일평균판매"] = (df["sales_30d"] / 30).round(2)
    # 총 가용재고 = 현재재고 + 쿠팡입고예정 + 키다리입고예정
    df["총가용재고"] = df["current_stock"] + df["coupang_incoming"] + df["keydari_incoming"]
    # 소진예상일수 (0 나누기 방지)
    df["소진예상일"] = df.apply(
        lambda r: round(r["총가용재고"] / r["일평균판매"], 1) if r["일평균판매"] > 0 else None,
        axis=1,
    )
    return df


# ---------------------------------------------------------------------------
# 사이드바 내비게이션
# ---------------------------------------------------------------------------
st.sidebar.title("📦 로켓그로스 관리")
page = st.sidebar.radio(
    "메뉴",
    ["재발주 대시보드", "발주 생성", "발주 기록 / 입고 처리", "상품 마스터", "재고 데이터 업로드"],
)

# ---------------------------------------------------------------------------
# 1. 재발주 대시보드
# ---------------------------------------------------------------------------
if page == "재발주 대시보드":
    st.title("재발주 대시보드")
    st.caption("현재재고 + 입고예정재고 + 최근 판매량을 기준으로 발주가 필요한 상품을 확인합니다.")

    df = load_reorder_view()
    if df.empty:
        st.info("등록된 상품이 없습니다. '상품 마스터' 메뉴에서 먼저 상품을 등록/가져오기 하세요.")
    else:
        col1, col2, col3 = st.columns(3)
        threshold_days = col1.number_input("경고 기준 (소진예상일 이하)", value=14, step=1)
        size_filter = col2.multiselect("사이즈 필터", sorted(df["size_tag"].dropna().unique().tolist()))
        show_only_alert = col3.checkbox("경고 대상만 보기", value=True)

        view = df.copy()
        if size_filter:
            view = view[view["size_tag"].isin(size_filter)]
        if show_only_alert:
            view = view[(view["소진예상일"].notna()) & (view["소진예상일"] <= threshold_days) |
                        (view["소진예상일"].isna() & (view["현재재고"] if "현재재고" in view else False))]
            # 재고 0 + 판매기록 없는 것도 경고에 포함
            view = pd.concat([
                view,
                df[(df["총가용재고"] <= 0)]
            ]).drop_duplicates(subset=["option_code"])

        st.metric("경고 대상 상품 수", len(view))

        display_cols = {
            "option_code": "옵션코드", "product_name": "상품명", "option_name": "옵션명",
            "size_tag": "사이즈", "current_stock": "현재재고", "coupang_incoming": "쿠팡입고예정",
            "keydari_incoming": "키다리입고예정", "총가용재고": "총가용재고",
            "sales_7d": "7일판매", "sales_30d": "30일판매", "소진예상일": "소진예상일",
        }
        st.dataframe(
            view[list(display_cols.keys())].rename(columns=display_cols),
            use_container_width=True, height=500,
        )
        st.download_button(
            "현재 목록 CSV로 내보내기",
            view.to_csv(index=False).encode("utf-8-sig"),
            file_name="재발주_대시보드.csv",
        )

# ---------------------------------------------------------------------------
# 2. 발주 생성
# ---------------------------------------------------------------------------
elif page == "발주 생성":
    st.title("발주 생성")
    df = load_reorder_view()

    if df.empty:
        st.info("등록된 상품이 없습니다. 먼저 상품 마스터를 가져오세요.")
    else:
        st.write("발주할 상품과 수량을 입력한 뒤 '발주 확정'을 누르면 발주회차로 저장됩니다.")

        edit_df = df[["option_code", "product_name", "option_name", "size_tag",
                       "current_stock", "coupang_incoming", "keydari_incoming", "소진예상일"]].copy()
        edit_df["발주수량"] = 0

        edited = st.data_editor(
            edit_df.rename(columns={
                "option_code": "옵션코드", "product_name": "상품명", "option_name": "옵션명",
                "size_tag": "사이즈", "current_stock": "현재재고",
                "coupang_incoming": "쿠팡입고예정", "keydari_incoming": "키다리입고예정",
            }),
            disabled=["옵션코드", "상품명", "옵션명", "사이즈", "현재재고", "쿠팡입고예정", "키다리입고예정", "소진예상일"],
            use_container_width=True, height=450, key="order_editor",
        )

        warehouse_code = st.text_input("입고창고 코드 (예: 동탄1)", value="동탄1")
        delivery_type = st.selectbox("입고구분", ["쿠팡", "키다리", "샘플"])
        delivery_method = st.selectbox("입고방법", ["택배", "화물"])
        memo = st.text_input("발주 메모", value="")

        to_order = edited[edited["발주수량"] > 0]
        st.write(f"발주 대상: {len(to_order)}건 / 총 수량: {int(to_order['발주수량'].sum()) if len(to_order) else 0}개")

        if st.button("✅ 발주 확정", type="primary", disabled=to_order.empty):
            conn = get_conn()
            cur = conn.cursor()
            today = date.today().isoformat()
            batch_no = next_batch_no(today)
            cur.execute(
                "INSERT INTO order_batches (batch_date, batch_no, memo) VALUES (?,?,?)",
                (today, batch_no, memo),
            )
            batch_id = cur.lastrowid

            price_map = dict(zip(df["option_code"], df["price_cny"]))
            for _, row in to_order.iterrows():
                unit_price = price_map.get(row["옵션코드"]) or 0
                qty = int(row["발주수량"])
                cur.execute("""
                    INSERT INTO order_items (
                        batch_id, option_code, warehouse_code, delivery_type,
                        delivery_method, order_qty, unit_price_cny, order_amount, memo
                    ) VALUES (?,?,?,?,?,?,?,?,?)
                """, (
                    batch_id, row["옵션코드"], warehouse_code, delivery_type,
                    delivery_method, qty, unit_price, qty * unit_price, memo,
                ))
            db_commit(conn)
            conn.close()
            st.success(f"{today} {batch_no}차 발주로 {len(to_order)}건이 저장되었습니다.")
            st.rerun()

# ---------------------------------------------------------------------------
# 3. 발주 기록 / 입고 처리
# ---------------------------------------------------------------------------
elif page == "발주 기록 / 입고 처리":
    st.title("발주 기록 / 입고 처리")
    conn = get_conn()
    batches = pd.read_sql_query(
        "SELECT * FROM order_batches ORDER BY batch_date DESC, batch_no DESC", conn
    )
    if batches.empty:
        st.info("발주 기록이 없습니다.")
    else:
        label_map = {
            row["batch_id"]: f"{row['batch_date']} {row['batch_no']}차 - {row['memo'] or ''}"
            for _, row in batches.iterrows()
        }
        batch_id = st.selectbox(
            "발주 회차 선택", options=list(label_map.keys()),
            format_func=lambda x: label_map[x],
        )

        items = pd.read_sql_query("""
            SELECT oi.order_item_id, oi.option_code, p.product_name, p.option_name,
                   oi.warehouse_code, oi.delivery_type, oi.delivery_method,
                   oi.order_qty, oi.unit_price_cny, oi.order_amount, oi.status
            FROM order_items oi
            LEFT JOIN products p ON p.option_code = oi.option_code
            WHERE oi.batch_id = ?
        """, conn, params=(batch_id,))

        st.dataframe(items.rename(columns={
            "option_code": "옵션코드", "product_name": "상품명", "option_name": "옵션명",
            "warehouse_code": "입고창고", "delivery_type": "입고구분", "delivery_method": "입고방법",
            "order_qty": "발주수량", "unit_price_cny": "단가(위안)", "order_amount": "발주금액", "status": "상태",
        }), use_container_width=True)

        st.download_button(
            "이 회차 발주서 CSV 다운로드",
            items.to_csv(index=False).encode("utf-8-sig"),
            file_name=f"발주서_{label_map[batch_id]}.csv",
        )

        st.divider()
        st.subheader("입고 처리")
        pending = items[items["status"] != "입고완료"]
        if pending.empty:
            st.success("이 회차는 모두 입고 처리되었습니다.")
        else:
            sel_item = st.selectbox(
                "입고 처리할 품목", options=pending["order_item_id"].tolist(),
                format_func=lambda i: f"{pending[pending.order_item_id==i]['상품명'].values[0] if '상품명' in pending else pending[pending.order_item_id==i]['product_name'].values[0]} / 발주수량 {pending[pending.order_item_id==i]['order_qty'].values[0]}"
                if False else str(i),
            )
            row = pending[pending["order_item_id"] == sel_item].iloc[0]
            st.write(f"옵션코드: **{row['옵션코드'] if '옵션코드' in row else row['option_code']}**  |  발주수량: **{row['발주수량'] if '발주수량' in row else row['order_qty']}**")
            received_qty = st.number_input("입고수량", min_value=0, value=int(row.get("발주수량", row.get("order_qty", 0))))
            box_no = st.text_input("박스번호", value="")
            received_date = st.date_input("입고일", value=date.today())

            if st.button("입고 확정"):
                cur = conn.cursor()
                cur.execute("""
                    INSERT INTO receiving_records (order_item_id, received_qty, received_date, box_no)
                    VALUES (?,?,?,?)
                """, (int(sel_item), int(received_qty), received_date.isoformat(), box_no))
                # 입고수량이 발주수량 이상이면 완료 처리, 아니면 부분입고로 표시
                order_qty = int(row.get("발주수량", row.get("order_qty", 0)))
                new_status = "입고완료" if received_qty >= order_qty else "부분입고"
                cur.execute(
                    "UPDATE order_items SET status = ? WHERE order_item_id = ?",
                    (new_status, int(sel_item)),
                )
                # 현재재고 반영
                cur.execute("""
                    UPDATE coupang_stock_snapshot
                    SET stock_sellable = stock_sellable + ?
                    WHERE option_code = ?
                """, (int(received_qty), row.get("옵션코드", row.get("option_code"))))
                db_commit(conn)
                st.success("입고 처리가 완료되었습니다. 재고가 갱신되었습니다.")
                st.rerun()
    conn.close()

# ---------------------------------------------------------------------------
# 4. 상품 마스터
# ---------------------------------------------------------------------------
elif page == "상품 마스터":
    st.title("상품 마스터 관리")
    conn = get_conn()
    products = pd.read_sql_query("SELECT * FROM products ORDER BY product_code", conn)
    conn.close()

    tab1, tab2 = st.tabs(["목록 조회", "엑셀로 가져오기"])

    with tab1:
        st.dataframe(products, use_container_width=True, height=500)

    with tab2:
        st.write(
            "기존 워크북의 **'단일상품'** 시트와 같은 형식(상품코드, 옵션코드, 구성개수, 상품명, "
            "옵션명, 이미지주소(발주), 1688주소, 업체명, 제품명(중문), 옵션명(중문), 가격(위안화), "
            "사이즈, 전략상품, 단종, 발주메모)의 엑셀 파일을 업로드하세요."
        )
        f = st.file_uploader("상품 마스터 엑셀 업로드", type=["xlsx", "xlsm", "xls"], key="product_upload")
        if f is not None and st.button("가져오기 실행"):
            try:
                n = importer.import_product_master(f)
                st.success(f"{n}건의 상품 정보를 가져왔습니다.")
                st.rerun()
            except Exception as e:
                st.error(f"가져오기 실패: {e}")

# ---------------------------------------------------------------------------
# 5. 재고 데이터 업로드
# ---------------------------------------------------------------------------
elif page == "재고 데이터 업로드":
    st.title("쿠팡 재고/판매 데이터 업로드")
    st.caption(
        "쿠팡 윙(Wing)에서 다운로드한 상품 리스트 파일을 그대로 업로드하면, "
        "기존 스냅샷을 통째로 교체합니다 (원본 매크로 'UpdateCoupangStockQuietly'와 동일 동작)."
    )

    f1 = st.file_uploader("쿠팡업로드상품 형식 파일 (옵션코드/재고/판매량)", type=["xlsx", "xls"], key="stock_upload")
    if f1 is not None and st.button("쿠팡 재고 데이터 갱신"):
        try:
            n = importer.import_coupang_stock(f1)
            st.success(f"{n}건의 재고/판매 데이터를 갱신했습니다.")
        except Exception as e:
            st.error(f"가져오기 실패: {e}")

    st.divider()
    f2 = st.file_uploader("키다리쿠팡입고 형식 파일 (옵션코드/입고수량)", type=["xlsx", "xls"], key="keydari_upload")
    if f2 is not None and st.button("키다리 입고예정 데이터 갱신"):
        try:
            n = importer.import_keydari_incoming(f2)
            st.success(f"{n}건의 입고예정 데이터를 갱신했습니다.")
        except Exception as e:
            st.error(f"가져오기 실패: {e}")
