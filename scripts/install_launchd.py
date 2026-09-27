"""Install the bot as a per-user macOS launch agent using the uv environment."""

from __future__ import annotations

import os
import plistlib
import shutil
import subprocess
import time
from pathlib import Path


LABEL = "com.gonge1018.compy-bot"
ROOT = Path(__file__).resolve().parents[1]
DOMAIN = f"gui/{os.getuid()}"
AGENT_PATH = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def main() -> None:
    if not (ROOT / ".env").is_file():
        raise SystemExit(".env 파일이 없습니다. 먼저 API 키와 봇 토큰을 설정하세요.")
    images = [
        path
        for path in (ROOT / "photos").iterdir()
        if path.is_file() and path.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp"}
    ]
    if len(images) < 2:
        raise SystemExit("photos/에 참조 사진을 두 장 이상 추가하세요.")
    uv = shutil.which("uv") or "/opt/homebrew/bin/uv"
    subprocess.run([uv, "sync", "--frozen"], cwd=ROOT, check=True)

    data_dir = ROOT / "data"
    data_dir.mkdir(exist_ok=True)
    AGENT_PATH.parent.mkdir(parents=True, exist_ok=True)
    agent = {
        "Label": LABEL,
        "ProgramArguments": [
            "/usr/bin/caffeinate",
            "-s",
            str(ROOT / ".venv/bin/python"),
            "bot.py",
            "run",
        ],
        "WorkingDirectory": str(ROOT),
        "EnvironmentVariables": {"BOT_DATA_DIR": str(data_dir)},
        "RunAtLoad": True,
        "KeepAlive": True,
        "ThrottleInterval": 30,
        "StandardOutPath": str(data_dir / "bot.stdout.log"),
        "StandardErrorPath": str(data_dir / "bot.stderr.log"),
    }
    subprocess.run(["launchctl", "bootout", f"{DOMAIN}/{LABEL}"], check=False, capture_output=True)
    with AGENT_PATH.open("wb") as stream:
        plistlib.dump(agent, stream)
    for attempt in range(5):
        result = subprocess.run(
            ["launchctl", "bootstrap", DOMAIN, str(AGENT_PATH)],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            break
        if attempt == 4:
            raise SystemExit(f"launchd 등록 실패: {result.stderr.strip()}")
        time.sleep(1)
    print(f"정훈봇 launchd 등록 완료: {AGENT_PATH}")
    print(f"상태: launchctl print {DOMAIN}/{LABEL}")
    print(f"로그: {data_dir / 'bot.stderr.log'}")


if __name__ == "__main__":
    main()
