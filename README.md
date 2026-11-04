# flow-cli

Google Flow (Imagen / Veo) 자동화를 위한 CLI 래퍼입니다. 오픈소스 [ffroliva/gflow-cli](https://github.com/ffroliva/gflow-cli) (MIT) 를 기반으로, 로컬 Dachapgo 브라우저 서버(CDP) 연동 브리지(`gflow_cli/dachapgo_bridge.py`)와 `dachapgo-status` / `dachapgo-stop` 서브커맨드를 추가했습니다.

## 구성
- `gflow.py` / `gflow.cmd` — 엔트리포인트 (Windows 는 `gflow.cmd`)
- `_upstream/gflow-cli/` — Python Flow CLI 코어 (수정본, MIT, © Flavio Oliva)
- `_upstream/google-flow-cli/` — Node.js / MCP Flow CLI (MIT, © Zahed Rastgar)

## 설치
```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
playwright install chromium
cp .env.example .env   # 값 채우기
python gflow.py --help
```

Dachapgo 연동을 쓰지 않으면 `DACHAPGO_*` 값은 비워 두면 되고, 표준 `gflow` 명령(`image t2i`, `video i2v`, `auth login` 등)은 그대로 동작합니다.

## 라이선스
각 업스트림 디렉터리의 `LICENSE`(MIT)를 따릅니다.
