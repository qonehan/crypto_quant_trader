# BTC Quant Bot - Prototype v0

## Quick Start (Codespaces)

1. **Open in Codespaces** - DB(Postgres)가 자동으로 실행됩니다.
2. **봇 실행**:
   ```bash
   poetry run python -m app.bot
   ```
   `Boot OK` / `DB OK` 출력 확인.
3. **대시보드 실행**:
   ```bash
   poetry run streamlit run app/dashboard.py
   ```
   포트 8501로 접속하여 화면 확인.

## Troubleshooting

- 대시보드에 `db` resolve 에러가 뜨면: **Dev Containers: Rebuild and Reopen in Container** 수행

## Notes

- `.env`는 커밋하지 마세요. `.env.example`만 제공됩니다.
- Codespaces `postCreateCommand`가 `.env.example` → `.env` 자동 복사를 처리합니다.
- DB 호스트는 compose 서비스명 `db`를 사용합니다.
