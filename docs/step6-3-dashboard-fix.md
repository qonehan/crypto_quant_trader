# Step 6-3 Dashboard Fix Report

**작성일:** 2026-02-18
**환경:** GitHub Codespaces / Dev Container (Python 3.11, Poetry 2.3.2)

---

## 1. 문제 요약

### 에러 메시지
```
ModuleNotFoundError: No module named 'app'
File ".../app/dashboard.py", line 8, in <module>
    from app.config import load_settings
```

### 발생 환경
| 항목 | 값 |
|---|---|
| 환경 | GitHub Codespaces / Dev Container |
| 실행 명령 | `streamlit run app/dashboard.py` (system streamlit 직접 호출) |
| poetry 상태 | 설치됨 (`Poetry 2.3.2`) |
| system python | `/usr/local/bin/python` (Python 3.11.13) |
| system streamlit | `/home/vscode/.local/bin/streamlit` (1.54.0) |
| poetry virtualenv | `/home/vscode/.cache/pypoetry/virtualenvs/btc-quant-bot-TBEOtHBJ-py3.11/` |

---

## 2. 원인 분석

### 진단 명령 출력

**① pwd / 프로젝트 루트 확인**
```
/workspaces/crypto_quant_trader
app/ 폴더 존재 확인 ✅
```

**② which streamlit**
```
/home/vscode/.local/bin/streamlit   ← system streamlit (poetry venv 아님)
```

**③ import app 가능 여부**
```
python -c "import app; print(...)"       → system python import OK  ✅
poetry run python -c "import app; ..."   → poetry python import OK  ✅
```

**④ 각 streamlit이 사용하는 python/경로**
```
system streamlit:
  __file__ = /home/vscode/.local/lib/python3.11/site-packages/streamlit/__init__.py
  sys.executable = /usr/local/bin/python

poetry streamlit:
  __file__ = /home/vscode/.cache/pypoetry/virtualenvs/.../streamlit/__init__.py
  sys.executable = /home/vscode/.cache/pypoetry/.../bin/python
```

### 최종 원인 결론

`dashboard.py`는 `from app.config import load_settings`를 line 8에서 호출한다.
Python은 실행 시 **현재 작업 디렉터리(CWD)** 와 `sys.path`에서 모듈을 탐색한다.

`ModuleNotFoundError: No module named 'app'`는 **두 가지 경우** 중 하나에서 발생한다:

| 원인 | 설명 |
|---|---|
| **원인 A** | 프로젝트 루트(`/workspaces/crypto_quant_trader`)가 아닌 다른 경로에서 `streamlit run app/dashboard.py` 실행 → `app` 패키지를 찾지 못함 |
| **원인 B** | `streamlit`을 `poetry run` 없이 system 전역으로 실행할 경우, virtualenv sys.path가 적용되지 않아 프로젝트 루트가 누락될 수 있음 |

`dashboard.py`에 sys.path 자동 보정 코드가 없었으므로, CWD가 달라지거나 외부에서 호출되면 곧바로 에러가 발생하는 구조였다.

---

## 3. 해결 과정

### 시도 순서

| 단계 | 방법 | 결과 |
|---|---|---|
| 해결 1 | `poetry run streamlit run` from 프로젝트 루트 | 정상 동작 확인 ✅ |
| 해결 4 | `dashboard.py` 최상단 sys.path 보정 추가 | 재발 방지 적용 ✅ |

> 해결 2(PYTHONPATH 환경변수), 해결 3(devcontainer rebuild)는 별도 적용 없이도 해결됐으나,
> 재발 방지를 위해 해결 4(코드 최소 수정)를 병행 적용함.

### 최종 적용 방법

**[dashboard.py 수정 — 최상단 sys.path 보정]**

`app/dashboard.py` 1~7번 줄에 아래 코드 추가 (기존 `import json` 위에):

```python
import os
import sys

# sys.path 보정: 어떤 경로에서 실행해도 프로젝트 루트를 찾을 수 있게 한다.
_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
```

`__file__`은 `dashboard.py`의 **절대 경로**이므로, 어떤 디렉터리에서 실행하든 프로젝트 루트를 정확히 가리킨다. 기존 코드는 전혀 수정하지 않았다.

**[실행 명령 — poetry run 사용]**

```bash
cd /workspaces/crypto_quant_trader
poetry run streamlit run app/dashboard.py \
  --server.address 0.0.0.0 \
  --server.port 8501 \
  --server.headless true
```

---

## 4. 최종 검증

### poetry run python import app
```
poetry run python -c "import app; print('poetry import app OK')"
→ poetry import app OK ✅
```

### curl http://localhost:8501
```
HTTP/1.1 200 OK
Server: TornadoServer/6.5.4
Content-Type: text/html
...
→ Streamlit HTML 정상 반환 ✅
```

### /healthz 엔드포인트
```
curl -s -o /dev/null -w "HTTP %{http_code}" http://localhost:8501/healthz
→ HTTP 200 ✅
```

### Streamlit 실행 로그
```
You can now view your Streamlit app in your browser.

  Local URL:    http://localhost:8501
  Network URL:  http://172.18.0.3:8501
  External URL: http://23.97.62.133:8501
```

### Codespaces 포트 렌더링
- VS Code **Ports** 탭에서 `8501` 포트 포워딩 확인
- **Open in Browser** 클릭 시 대시보드 UI 정상 로드

---

## 5. 재발 방지 체크리스트

### 실행 전 확인
- [ ] 현재 디렉터리가 프로젝트 루트인지 확인: `pwd` → `/workspaces/crypto_quant_trader`
- [ ] 반드시 `poetry run streamlit run app/dashboard.py` 형식으로 실행
- [ ] `streamlit run app/dashboard.py` (bare) 사용 금지 — system streamlit이 호출될 수 있음

### ModuleNotFoundError 발생 시 체크 순서

```bash
# 1. 경로 확인
pwd   # → /workspaces/crypto_quant_trader 이어야 함

# 2. poetry import 테스트
poetry run python -c "import app; print('OK')"

# 3. poetry run으로 재실행
poetry run streamlit run app/dashboard.py --server.address 0.0.0.0 --server.port 8501 --server.headless true

# 4. 그래도 실패 시 PYTHONPATH 강제
PYTHONPATH=/workspaces/crypto_quant_trader poetry run streamlit run app/dashboard.py \
  --server.address 0.0.0.0 --server.port 8501 --server.headless true

# 5. poetry 명령 자체가 없으면 Dev Container 재오픈
# Command Palette → "Dev Containers: Rebuild and Reopen in Container"
```

### 코드 수준 방어 (이번에 적용됨)
- `dashboard.py` 최상단 `sys.path` 보정 추가 완료
- 이제 `streamlit run app/dashboard.py`를 어느 경로에서 실행해도 `app` 패키지를 찾을 수 있음
- 단, **poetry 환경에서 실행하는 것을 권장**함 (의존성 격리 보장)

### README 기준 정석 실행법
```bash
# 봇 (터미널 1)
poetry run python -m app.bot

# 대시보드 (터미널 2)
poetry run streamlit run app/dashboard.py
# → http://localhost:8501
```
