# 로켓그로스 발주/입고 관리 (v0.1 - 발주/입고 모듈)

기존 엑셀 매크로 워크북 중 **발주 및 입고 관리** 기능을 독립 웹앱으로 재구현한 1차 버전입니다.

## 포함된 기능
- 상품 마스터 관리 (엑셀 업로드로 일괄 등록/갱신)
- 쿠팡 재고·판매 데이터 업로드 (쿠팡 다운로드 파일을 그대로 업로드 → 자동 반영)
- 재발주 대시보드 (현재재고 + 입고예정 + 최근 판매량 기준 경고)
- 발주 생성 및 발주회차별 기록
- 입고 처리 (입고 확정 시 재고 자동 갱신)

## 아직 포함되지 않은 것 (다음 단계)
- 마진 계산, 정산 자동취합, 조합상품/바코드, 광고 관리 등 나머지 매크로 기능
- 쿠팡 오픈API 자동 연동 (현재는 파일 업로드 방식)
- 개인별 로그인 (현재는 팀 공용 비밀번호 1개)

---

## 1. 로컬에서 먼저 테스트하기

```bash
pip install -r requirements.txt
streamlit run app.py
```

브라우저에서 `http://localhost:8501` 접속.

---

## 2. 배포 방법

### 지금 단계(혼자, 컴퓨터 2대) 추천: Streamlit Community Cloud + Turso (무료)

Turso는 무료로 쓸 수 있는 클라우드 SQLite(libSQL) 서비스입니다. 이 앱은 Turso 접속 정보가
환경변수에 있으면 자동으로 그쪽을 쓰고, 없으면 그냥 로컬 파일을 씁니다 (코드 수정 불필요).

1. **Turso 가입 및 DB 생성**
   - https://turso.tech 가입 (무료 플랜으로 충분)
   - CLI 또는 대시보드에서 데이터베이스 하나 생성 (예: `rocket-growth`)
   - `Database URL` 과 `Auth Token` 발급받기

2. **GitHub에 이 폴더 업로드** (비공개 저장소로)

3. **Streamlit Community Cloud 배포**
   - https://share.streamlit.io 접속 → 방금 만든 저장소 연결 → `app.py` 지정해서 배포
   - **Settings → Secrets** 에 아래 추가:
     ```
     TURSO_DATABASE_URL = "libsql://your-db-name.turso.io"
     TURSO_AUTH_TOKEN = "발급받은 토큰"
     APP_PASSWORD = "원하는 비밀번호"
     ```
   - **Settings → Sharing** 에서 "Who can view this app"을 특정 이메일로 제한하면
     구글에 검색해도 안 나오고, 초대한 사람만 접속 가능합니다.

4. 배포된 주소(`xxx.streamlit.app`)를 즐겨찾기 해두고 컴퓨터 2대에서 각각 접속하면 됩니다.
   서버를 켜둘 필요가 없고, 비용도 0원입니다.

이 방식의 한계: 동시에 너무 많은 인원이 몰리면(수십 명) 무료 플랜 한도에 걸릴 수 있습니다.
그때가 되면 아래 유료 서버로 넘어가면 됩니다 — **코드는 그대로 두고 환경변수만 옮기면 됩니다.**

### 나중에 팀 규모가 커지면: Railway / Render (유료, 월 5천원~1만원대)

같은 코드, 같은 Turso DB(또는 자체 DB)를 그대로 쓰면서 호스팅만 상시 가동형 유료 서버로 옮기면 됩니다.

1. https://railway.app 또는 https://render.com 가입
2. GitHub 저장소 연결해서 배포
3. **Variables(환경변수)** 에 동일하게 `TURSO_DATABASE_URL`, `TURSO_AUTH_TOKEN`, `APP_PASSWORD` 설정
   (또는 이 시점에 Turso 대신 자체 Postgres로 옮겨도 됨 — 그 경우 db.py 수정 필요)
4. Start Command: `streamlit run app.py --server.port=$PORT --server.address=0.0.0.0`

### 참고: 그냥 로컬 동기화로 쓰고 싶다면 (완전 무료, 대신 동시접속 금지)

Turso 없이 환경변수를 아예 설정하지 않으면 로컬 SQLite 파일(`rocket_growth.db`)을 씁니다.
이 파일을 Dropbox/OneDrive 같은 동기화 폴더에 두고 컴퓨터 2대에서 각자 `streamlit run app.py`를
실행하면 됩니다. 단, **두 컴퓨터에서 동시에 열지 마세요** — 동기화 충돌로 데이터가 깨질 수 있습니다.


---

## 3. 데이터 가져오기 순서

1. **상품 마스터** 메뉴 → 기존 워크북의 '단일상품' 시트를 엑셀로 내보내서 업로드
2. **재고 데이터 업로드** 메뉴 → 쿠팡 윙에서 다운로드한 상품 리스트 파일 업로드
3. **재발주 대시보드**에서 경고 대상 확인 → **발주 생성**에서 수량 입력 후 확정
4. **발주 기록 / 입고 처리**에서 회차별 발주서 확인, 물건 도착 시 입고 처리

## 파일 구성
- `app.py` : Streamlit 화면 및 로직
- `db.py` : SQLite 스키마 정의
- `importer.py` : 엑셀 → DB 가져오기 함수
- `requirements.txt`, `Procfile`, `.streamlit/config.toml` : 배포용 설정
