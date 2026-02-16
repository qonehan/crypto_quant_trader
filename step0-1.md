# Step 0-1 Hotfix 결과 보고서

## 문제

Streamlit 대시보드에서 DB 연결 시 `failed to resolve host 'db'` 에러 발생.
Codespaces가 docker-compose 네트워크 밖에서 실행되거나, db 컨테이너가 포트 충돌로 시작 실패하여 DNS에 "db"가 등록되지 않는 것이 원인.

## 수정한 파일 목록

| # | 파일 경로 | 변경 내용 |
|---|----------|----------|
| 1 | `.devcontainer/devcontainer.json` | `runServices: ["db"]` 추가, `forwardPorts`에서 5432 제거 (8501만 유지) |
| 2 | `.devcontainer/docker-compose.yml` | db ports를 `5433:5432`로 변경 (충돌 방지), healthcheck 추가 (`pg_isready`) |
| 3 | `app/bot.py` | DB 연결 실패 시 `failed to resolve host` / `could not translate host name` 감지하여 진단 힌트 출력 |
| 4 | `app/dashboard.py` | 동일한 진단 힌트를 `st.warning()`으로 표시 |
| 5 | `README.md` | Troubleshooting 섹션 추가: db resolve 에러 시 Rebuild and Reopen 안내 |

## 변경 상세

### 1. `.devcontainer/devcontainer.json`

```json
{
  "runServices": ["db"],
  "forwardPorts": [8501]
}
```

- `runServices`로 db 서비스가 Codespaces 시작 시 반드시 기동됨
- 5432 포트 포워딩 제거 (app 컨테이너 포트만 포워딩 가능하므로 혼동 방지)

### 2. `.devcontainer/docker-compose.yml`

```yaml
db:
  ports:
    - "5433:5432"
  healthcheck:
    test: ["CMD-SHELL", "pg_isready -U postgres -d quant"]
    interval: 5s
    timeout: 5s
    retries: 20
```

- 호스트 포트를 5433으로 변경하여 Codespaces/로컬 환경의 5432 충돌 방지
- healthcheck로 db 컨테이너 준비 상태를 확인 가능

### 3. `app/bot.py` — 진단 메시지 강화

DB 연결 실패 시 에러에 `failed to resolve host` 또는 `could not translate host name`이 포함되면:
```
DB host 'db'를 찾지 못했습니다. Codespaces에서 Dev Containers: Rebuild and Reopen in Container를 실행해 docker-compose devcontainer로 들어가 있는지 확인하세요. 또한 db 컨테이너가 정상 실행 중인지 확인하세요.
```

### 4. `app/dashboard.py` — 진단 메시지 강화

동일한 조건에서 `st.warning()`으로 힌트를 표시.

### 5. `README.md` — Troubleshooting 추가

> 대시보드에 `db` resolve 에러가 뜨면: **Dev Containers: Rebuild and Reopen in Container** 수행

## 검증 결과

### 현재 환경 (compose 네트워크 밖)

현재 Codespaces는 docker-compose devcontainer로 rebuild되지 않은 상태이므로, `db` hostname이 해석되지 않음. 이는 예상된 동작임.

#### DNS 해석 테스트

```
$ poetry run python -c "import socket; print('db->', socket.gethostbyname('db'))"
socket.gaierror: [Errno -2] Name or service not known
```

→ compose 네트워크 밖이므로 실패 (예상됨)

#### bot.py 실행

```
$ poetry run python -m app.bot
DB connection failed: (psycopg.OperationalError) failed to resolve host 'db': ...
DB host 'db'를 찾지 못했습니다. Codespaces에서 Dev Containers: Rebuild and Reopen in Container를 실행해 docker-compose devcontainer로 들어가 있는지 확인하세요. 또한 db 컨테이너가 정상 실행 중인지 확인하세요.
```

→ 진단 힌트가 정상 출력됨

### 컨테이너 재빌드 후 검증 (수동 필요)

> **중요**: 아래 검증은 Codespaces에서 **Dev Containers: Rebuild and Reopen in Container** (또는 **Codespaces: Rebuild Container**) 실행 후 수행해야 합니다.

재빌드 후 실행할 검증 명령:

```bash
# 1. DNS 해석 테스트
poetry run python -c "import socket; print('db->', socket.gethostbyname('db'))"
# 기대: db-> 172.x.x.x (IP 출력)

# 2. Bot 실행
poetry run python -m app.bot
# 기대: Boot OK / DB OK

# 3. Dashboard 실행
poetry run streamlit run app/dashboard.py
# 기대: 8501 포트에서 DB Connection Test 성공
```

### 성공 기준

| 항목 | 기대 결과 |
|------|----------|
| `socket.gethostbyname('db')` | IP 주소 출력 |
| `python -m app.bot` | `Boot OK` / `DB OK` |
| Streamlit dashboard | DB Connection Test: 성공 (`st.success`) |

## 다음 단계

1. Codespaces에서 **Dev Containers: Rebuild and Reopen in Container** 실행
2. 위 검증 명령 3개 실행하여 성공 확인
