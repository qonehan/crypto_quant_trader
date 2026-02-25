"""activate_env_keys.py — .env 파일에서 주석 처리된 API 키를 활성화한다.

동작:
  1. .env 백업 (.env.bak.YYYYMMDD_HHMMSS)
  2. 대상 키 목록에 대해 "활성 라인 존재?" 검사
  3. 없으면 '# KEY=VALUE' 라인에서 값을 꺼내 활성 라인으로 append
  4. 이번 step에서 필요한 필수 키 존재 여부 최종 검사

사용법:
  python scripts/activate_env_keys.py
"""

from __future__ import annotations

import re
import shutil
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = ROOT / ".env"

# 활성화 대상 키 목록
CANDIDATE_KEYS = [
    "UPBIT_ACCESS_KEY",
    "UPBIT_SECRET_KEY",
    "COINGLASS_API_KEY",
    "BINANCE_API_KEY",  # 향후 사용 대비
]

# 이번 step에서 반드시 있어야 하는 키 (값이 비어도 OK — 단지 "라인 존재"가 기준)
# Coinglass key는 없어도 SKIP 처리되므로 필수가 아님
REQUIRED_ACTIVE_KEYS: list[str] = []


def _parse_env(lines: list[str]) -> dict[str, str]:
    """활성 KEY=VALUE 라인만 파싱해서 반환."""
    active: dict[str, str] = {}
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        active[key.strip()] = value.strip()
    return active


def _find_commented_value(lines: list[str], key: str) -> str | None:
    """'# KEY=VALUE' 형태의 주석 라인에서 VALUE를 추출."""
    pattern = re.compile(r"^\s*#\s*" + re.escape(key) + r"\s*=\s*(.+)$")
    for line in lines:
        m = pattern.match(line)
        if m:
            return m.group(1).strip()
    return None


def main() -> int:
    if not ENV_FILE.exists():
        print(f"ERROR: {ENV_FILE} not found")
        return 1

    # (1) 백업
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup = ENV_FILE.with_name(f".env.bak.{ts}")
    shutil.copy2(ENV_FILE, backup)
    print(f"Backup created: {backup}")

    lines = ENV_FILE.read_text(encoding="utf-8").splitlines(keepends=True)
    active = _parse_env(lines)

    appended: list[str] = []
    for key in CANDIDATE_KEYS:
        if key in active:
            print(f"  [{key}] already active — skip")
            continue
        value = _find_commented_value(lines, key)
        if value is None:
            print(f"  [{key}] no commented line found — skip")
            continue
        new_line = f"{key}={value}\n"
        lines.append(new_line)
        active[key] = value
        appended.append(key)
        print(f"  [{key}] activated from commented line")

    if appended:
        ENV_FILE.write_text("".join(lines), encoding="utf-8")
        print(f"Saved {ENV_FILE} ({len(appended)} key(s) activated)")
    else:
        print("No changes needed.")

    # (4) 필수 키 검사
    missing = [k for k in REQUIRED_ACTIVE_KEYS if k not in active or not active[k]]
    if missing:
        print(f"\nERROR: Required keys missing or empty: {missing}")
        return 1

    # 정보성 출력
    binance_key_set = bool(active.get("BINANCE_API_KEY", ""))
    coinglass_key_set = bool(active.get("COINGLASS_API_KEY", ""))
    upbit_key_set = bool(active.get("UPBIT_ACCESS_KEY", "")) and bool(
        active.get("UPBIT_SECRET_KEY", "")
    )
    print("\n  Key status:")
    print(f"    UPBIT_ACCESS/SECRET_KEY : {'SET' if upbit_key_set else 'NOT SET'}")
    print(f"    COINGLASS_API_KEY       : {'SET' if coinglass_key_set else 'NOT SET (Coinglass will SKIP)'}")
    print(f"    BINANCE_API_KEY         : {'SET' if binance_key_set else 'NOT SET (public endpoints OK)'}")
    print("\nactivate_env_keys: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
