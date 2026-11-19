# Run Vibe Trading on your PC

The code is in `C:\Users\xhu70\Documents\vibe-trading`. Run it from **Windows PowerShell**. You do not need Ubuntu / WSL for normal use.

WSL automatically stops the Ubuntu distro about one to two minutes after there is no attached terminal, which takes the backend, frontend, and Surfpool forks down with it. That is why the window seemed to vanish on its own.

`.env` and `secrets/` hold API keys and wallet private keys and are gitignored. Never commit or share either of them.

## First-time setup (Windows)

Install [Python 3.12+](https://www.python.org/downloads/) (tick "Add python.exe to PATH") and [Node.js 20+](https://nodejs.org/) if they are not already on Windows. Then in PowerShell:

```powershell
cd C:\Users\xhu70\Documents\vibe-trading
powershell -ExecutionPolicy Bypass -File .\scripts\dev.ps1 -Setup
```

This creates the Python venv, installs npm packages, and downloads the official Surfpool 1.7.0 Windows binary into `.tools\`. If Windows Defender blocks the unsigned Surfpool build, allow it and run `-Setup` again. The Windows Surfpool build needs OpenSSL 3 DLLs; `-Setup` copies them from Git for Windows.

## Every start

```powershell
cd C:\Users\xhu70\Documents\vibe-trading
powershell -ExecutionPolicy Bypass -File .\scripts\dev.ps1
```

The default is **rehearsal mode**: it replays the coin-discovery result and uses only cached research, so no real money is spent. Open the URL the script prints (usually http://127.0.0.1:5173) in Chrome or Edge, allow the microphone, and start talking.

If another Windows app is already using ports 8000 or 5173, the script picks a free port (for example 8010 / 5174) and prints that URL. Use the printed address, not a remembered one.

You can close the PowerShell window; the app keeps running in the background. To stop the UI and backend:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\stop_dev.ps1
```

To also stop the local forks (open fork positions are lost):

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\stop_surfpool.ps1
```

The full demo script is in `docs/M6_voice_demo.md`. Short version (speak English):

1. "I want to put in 50 dollars total, and at most 10 dollars per trade." (No fee needed: the default fee cap 0.002 SOL is applied and shown on the right. You can also say one, e.g. "max 20 cents in fees".)
2. The agent shows its proposed default coin filters on the right. Change any, e.g. "Set min liquidity to 80000 dollars.", or say "Looks good."
3. "Stop loss at 30 percent, and sell half when it hits 2x." (or "Stop loss 20 percent, take profit 30 percent" = sell everything at +30%). Then check the settings on the right and say "Confirm."
4. After research the **selection desk** fills the main stage: every passed coin as a tile with its numbers and a K-line chart. Pick any number: "Coin 1 and coin 3, 5 dollars each." (or tick them and click Create cards). Max per trade is only a cap.
5. One confirmation card per coin; say each two-digit code, e.g. "Approve 67." Just "Approve." will not trade.
6. While it waits the agent stays quiet. You can ask "What's the status?" or say "Sell all."
7. Rehearsal only: under Positions, **Test take profit** / **Test stop loss** push the fork price past your line so you can watch the automatic sell (or say "simulate take profit").
8. Duplex: you can talk while it works. Interrupt the agent any time; it stops and listens. While research runs, say "make it market cap below 300,000" to re-plan, or "stop the research" (or click **Stop research**). Buys and the emergency exit run in the background; the agent says "Buying now" and the fill comes as an update.
9. Keep going: after a sell, say "I want another research round with market cap below 400,000." The agent applies it and hunts again; cash, positions and history carry over. Guard changes (budget, per-trade, fees, default exits) need "confirm".

## Notes

- All trading happens on the local fork; your mainnet trading wallet is never touched.
- `.\scripts\dev.ps1 -Mode live` spends real x402 research fees (a few cents each) and asks again before starting. Do not add `-Mode live` when rehearsing.
- The rehearsal cache currently has full research data only for GATO; the other two coins fail in rehearsal mode. That is expected.
- The "Test AI agent" button in the ElevenLabs dashboard cannot run these frontend tools; test only through this page.
- Shadow listener: while you talk, a "Listening… / Hearing: …" strip shows what the backend already understood; it never acts on it. Filter changes apply about 1 second sooner.
- The agent runs on Eleven Realtime v1 Max (costs more per minute than Custom mode). To go back, from the repo in a terminal that has the venv: `python scripts/configure_elevenlabs_agent.py --custom`.
- Surfpool memory: the fork no longer grows while a position is held. After a long session, sell, then `.\scripts\stop_surfpool.ps1` and start again.

## Optional: Ubuntu (WSL), not required

The old `./scripts/dev.sh` path still works inside Ubuntu, but it is not the supported way to run a session on this PC. If the Ubuntu window closes, or WSL decides the distro is idle, everything inside it stops.

## 中文简要

在 Windows 的 PowerShell 里运行即可，不必再开 Ubuntu。第一次先执行 `.\scripts\dev.ps1 -Setup`（会安装 Python 依赖、前端依赖，并下载官方 Surfpool Windows 版），以后每次执行 `.\scripts\dev.ps1`（排练模式，不花真钱），用 Chrome 打开脚本打印出来的地址（一般是 http://127.0.0.1:5173 ）。如果你电脑上 8000 或 5173 已被别的程序占用，脚本会自动换端口并打印新地址，请用新地址。PowerShell 窗口可以关掉，后台会继续跑；要停就运行 `.\scripts\stop_dev.ps1`，连分叉一起停再用 `.\scripts\stop_surfpool.ps1`。Windows Defender 可能会拦截未签名的 Surfpool，允许即可。演示时用英文说话，步骤与以前相同。不要再依赖 Ubuntu 窗口：WSL 在没有挂着的终端时，大约一两分钟就会自动把 Ubuntu 停掉，里面的服务会一起没。

## Open it on your phone

The UI is voice-first and built for phones too (bottom sheets, safe areas, touch). The phone and the PC must be on the same Wi-Fi.

```powershell
.\scripts\dev.ps1 -Lan -Https      # Windows
```
```bash
scripts/dev.sh --lan --https       # Linux / macOS
```

The script prints the phone address, e.g. `https://192.168.1.23:5173`. Open it on the phone.

- **Why HTTPS:** phone browsers only give the microphone to `https://` pages (or `localhost`). `-Lan` alone works for text chat; voice needs `-Https` too.
- The certificate is self-signed (via `@vitejs/plugin-basic-ssl`, a dev dependency: run `npm install` in `frontend/` once after pulling). The phone shows a warning the first time: tap *Advanced* -> *Proceed* (Chrome) or *Show Details* -> *visit this website* (Safari).
- Windows may ask to allow Node.js through the firewall: allow it on *Private* networks.
- Without `-Lan` nothing changes: the UI only listens on 127.0.0.1.
- The backend still only listens on 127.0.0.1; the phone reaches it through the Vite proxy (`/api`).
