#!/usr/bin/env python3
"""
AUGUR Reporter — @Sammuraiibot / @Ariauugaurd (8339456128)
Read-only. DeepSeek primary. No code execution, no file writes.
Delivers signal summaries, trade alerts, and scheduled kingdom reports.
"""
import os, sys, time, json, threading, traceback, subprocess
from pathlib import Path
from openai import OpenAI
import requests as req

# ─── Config ──────────────────────────────────────────────────────────────────
DEEPSEEK_API_KEY = os.environ.get(
    'DEEPSEEK_API_KEY',
    'sk-d32b8d1d43464b6eb9474c818d00782d'
)
BOT_TOKEN = os.environ.get(
    'AUGUR_BOT_TOKEN',
    '8339456128:AAG3orDeO7AZWDEoKgJc1j2UF6AcquK_2Ic'
)
CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID', '7102469944')

TG_API  = f"https://api.telegram.org/bot{BOT_TOKEN}"
client  = OpenAI(api_key=DEEPSEEK_API_KEY, base_url='https://api.deepseek.com/v1')
MODEL   = "deepseek-chat"

# Paths (server)
_AUGUR_LOG     = "/home/dayodapper/AUGUR/logs/augur.log"
_AUGUR_INTEL   = "/home/dayodapper/AUGUR/logs/intelligence_signals.json"
_AUGUR_HOT     = "/home/dayodapper/AUGUR/logs/hot_signals.json"
_AUGUR_WALLETS = "/home/dayodapper/AUGUR/logs/smart_wallets.json"
_KINGDOM_STATE = "/home/dayodapper/kingdom/kingdom_state.json"
_ARIA_LOG      = "/home/dayodapper/ARIA/logs/aria.log"

_report_lock = threading.Lock()
_last_slot   = -1
_last_morning = -1
_last_eod     = -1

# ─── Logging ─────────────────────────────────────────────────────────────────
def log(msg):
    ts = time.strftime("%H:%M:%S", time.gmtime())
    print(f"[{ts}] {msg}", flush=True)

# ─── Shell (read-only ops only) ───────────────────────────────────────────────
def _run(cmd: str, timeout: int = 30) -> str:
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
        out = (r.stdout.strip() + ("\n" + r.stderr.strip() if r.stderr.strip() else "")).strip()
        return out or "(no output)"
    except subprocess.TimeoutExpired:
        return f"[timeout after {timeout}s]"
    except Exception as e:
        return f"[error: {e}]"

# ─── Telegram ─────────────────────────────────────────────────────────────────
def tg(chat_id, text, parse_mode="Markdown"):
    if not text or not text.strip():
        return
    for chunk in [text[i:i+3800] for i in range(0, len(text), 3800)]:
        try:
            r = req.post(f"{TG_API}/sendMessage",
                        json={"chat_id": chat_id, "text": chunk, "parse_mode": parse_mode},
                        timeout=15)
            if not r.ok:
                req.post(f"{TG_API}/sendMessage",
                        json={"chat_id": chat_id, "text": chunk},
                        timeout=15)
        except Exception as e:
            log(f"[tg error] {e}")

# ─── Data collectors ──────────────────────────────────────────────────────────
def _get_augur_snapshot() -> str:
    augur = _run(f"tail -60 {_AUGUR_LOG} 2>/dev/null")
    bal   = _run(f"grep bybit_balance_fetched {_AUGUR_LOG} 2>/dev/null | tail -3")
    blk   = _run(f"grep strategy_insufficient_balance {_AUGUR_LOG} 2>/dev/null | tail -5")
    evl   = _run(f"grep strategy_evaluated {_AUGUR_LOG} 2>/dev/null | tail -10")
    try:
        hot_raw  = Path(_AUGUR_HOT).read_text()
        cold_raw = Path(_AUGUR_INTEL).read_text()
        kst_raw  = _run(f"cat {_KINGDOM_STATE} | python3 -m json.tool 2>/dev/null | head -60")
    except Exception:
        hot_raw = cold_raw = kst_raw = "(unavailable)"
    return (f"AUGUR LOG:\n{augur}\n\nBALANCE:\n{bal}\n\nBLOCKED:\n{blk}\n\n"
            f"EVALUATIONS:\n{evl}\n\nHOT SIGNALS:\n{hot_raw[:2000]}\n\n"
            f"COLD SIGNALS:\n{cold_raw[:2000]}\n\nKINGDOM:\n{kst_raw}")

def _llm(system_p, user_p, max_tokens=500) -> str:
    try:
        r = client.chat.completions.create(
            model=MODEL,
            messages=[{"role": "system", "content": system_p},
                      {"role": "user",   "content": user_p[:40_000]}],
            max_tokens=max_tokens,
            timeout=60,
        )
        return r.choices[0].message.content or "(empty)"
    except Exception as e:
        return f"[error: {e}]"

# ─── Report generators ────────────────────────────────────────────────────────
_REPORT_SYS = """You are AUGUR Reporter — concise quant analyst for a live perps trading kingdom.
Report AUGUR's state: signals, blocked trades, balance, chancellor decisions.
Under 300 words. Precise. Quote exact log values. No fluff."""

def generate_report() -> str:
    data = _get_augur_snapshot()
    utc  = time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime())
    return _llm(_REPORT_SYS,
                f"UTC: {utc}\n\n{data}",
                max_tokens=500)

def generate_hot_signal_alert(signal: dict) -> str:
    arrow = "⬆" if signal.get("direction") == "long" else "⬇"
    sym   = signal.get("symbol", "?")
    dirn  = signal.get("direction", "?").upper()
    conv  = signal.get("conviction", 0)
    trig  = signal.get("trigger", "")
    lev   = signal.get("leverage_rec", 5)
    reas  = signal.get("reasoning", "")[:120]
    return (f"🔥 *HOT SIGNAL*\n"
            f"{arrow} *{sym}* `{dirn}` — {trig}\n"
            f"conviction={conv:.2f}  lev={lev}x\n"
            f"_{reas}_")

# ─── Signal monitor (polls hot_signals.json for new signals) ─────────────────
_seen_signals: set = set()

def _poll_hot_signals():
    global _seen_signals
    try:
        raw = Path(_AUGUR_HOT).read_text()
        data = json.loads(raw)
        sigs = data.get("signals", [])
        now_ms = int(time.time() * 1000)
        for s in sigs:
            sid = s.get("symbol", "") + s.get("direction", "") + str(s.get("expires_ms", 0))
            if sid not in _seen_signals and s.get("expires_ms", 0) > now_ms:
                _seen_signals.add(sid)
                alert = generate_hot_signal_alert(s)
                tg(CHAT_ID, alert)
                log(f"[hot alert] {s.get('symbol')} {s.get('direction')}")
        # prune expired from seen set
        _seen_signals = {sid for sid in _seen_signals
                         if any(sid.endswith(str(s.get("expires_ms", 0))) and s.get("expires_ms", 0) > now_ms
                                for s in sigs)}
    except Exception:
        pass

# ─── Message handler (read-only commands only) ────────────────────────────────
def handle_message(chat_id, text):
    try:
        if CHAT_ID and str(chat_id) != str(CHAT_ID):
            tg(chat_id, "Unauthorized.")
            return
        text = text.strip()
        log(f"[msg] {chat_id}: {text[:80]}")

        if text in ('/start', '/help'):
            tg(chat_id, (
                "📡 *AUGUR Reporter*\n\n"
                "_Read-only signal & status bot._\n\n"
                "`/status` — quick health check\n"
                "`/signals` — hot + cold signals\n"
                "`/report` — full kingdom report\n"
                "`/balance` — AUGUR Bybit balance\n"
                "`/kingdom` — kingdom state\n\n"
                "_For operations, use @Dapppperbot_"
            ))
            return

        if text == '/status':
            tg(chat_id, "🔍 Checking...")
            augur = _run(f"tail -5 {_AUGUR_LOG} 2>/dev/null")
            bal   = _run(f"grep bybit_balance_fetched {_AUGUR_LOG} 2>/dev/null | tail -2")
            tg(chat_id, f"*AUGUR*\n```\n{augur}\n```\n\n*Balance*\n```\n{bal}\n```")
            return

        if text == '/balance':
            bal = _run(f"grep bybit_balance_fetched {_AUGUR_LOG} 2>/dev/null | tail -3")
            tg(chat_id, f"*AUGUR Balance*\n```\n{bal}\n```")
            return

        if text == '/kingdom':
            kst = _run(f"cat {_KINGDOM_STATE} | python3 -m json.tool 2>/dev/null | head -60")
            tg(chat_id, f"*Kingdom State*\n```\n{kst[:3500]}\n```")
            return

        if text == '/signals':
            tg(chat_id, "📡 _Reading signals..._")
            lines = ["*AUGUR SIGNALS*"]
            try:
                hot_data = json.loads(Path(_AUGUR_HOT).read_text())
                hot_sigs = hot_data.get("signals", [])
                now_ms   = int(time.time() * 1000)
                active   = [s for s in hot_sigs if s.get("expires_ms", 0) > now_ms]
                if active:
                    lines.append(f"\n🔥 *HOT* ({len(active)} active)")
                    for s in active[:5]:
                        arrow = "⬆" if s.get("direction") == "long" else "⬇"
                        lines.append(f"{arrow} *{s.get('symbol')}* `{s.get('direction','').upper()}` "
                                     f"conv={s.get('conviction',0):.2f} lev={s.get('leverage_rec',5)}x "
                                     f"— _{s.get('trigger','')}_")
                else:
                    lines.append("\n🔥 *HOT*: none active")
            except Exception as e:
                lines.append(f"\n🔥 *HOT*: error — {e}")

            try:
                cold_data = json.loads(Path(_AUGUR_INTEL).read_text())
                cold_sigs = cold_data.get("signals", [])
                now_ms    = int(time.time() * 1000)
                active_c  = [s for s in cold_sigs if s.get("expires_ms", 0) > now_ms and s.get("confidence_boost", 0) > 0]
                gen_ms    = cold_data.get("generated_ms", 0)
                age_min   = round((int(time.time()*1000) - gen_ms) / 60000) if gen_ms else "?"
                if active_c:
                    lines.append(f"\n🧊 *COLD* (6h, {age_min}min ago)")
                    for s in active_c[:4]:
                        arrow = "⬆" if s.get("direction") == "long" else "⬇"
                        lines.append(f"{arrow} *{s.get('symbol')}* `{s.get('direction','').upper()}` "
                                     f"boost=+{s.get('confidence_boost',0):.2f} conv={s.get('conviction',0):.2f}")
                else:
                    lines.append(f"\n🧊 *COLD*: none boosted (last run {age_min}min ago)")
            except Exception as e:
                lines.append(f"\n🧊 *COLD*: error — {e}")

            tg(chat_id, "\n".join(lines))
            return

        if text == '/report':
            tg(chat_id, "🧠 _Generating report..._")
            report = generate_report()
            tg(chat_id, f"📊 *AUGUR REPORT*\n\n{report}")
            return

        # Anything else: simple DeepSeek Q&A with AUGUR context (read-only)
        tg(chat_id, "🔍 _Checking kingdom..._")
        data   = _get_augur_snapshot()
        utc    = time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime())
        system = (_REPORT_SYS +
                  "\n\nAnswer user questions about AUGUR's signals, trades, and status. "
                  "Read-only — do not suggest code changes or server commands. "
                  "For ops tasks, direct user to @Dapppperbot.")
        answer = _llm(system, f"UTC: {utc}\n\nDATA:\n{data[:20000]}\n\nQUESTION: {text}", max_tokens=400)
        tg(chat_id, answer)

    except Exception as e:
        log(f"[handle CRASH] {e}\n{traceback.format_exc()}")
        tg(chat_id, f"⚠️ Error: {e}")

# ─── Main polling loop ────────────────────────────────────────────────────────
def main():
    global _last_slot, _last_morning, _last_eod

    log("╔══════════════════════════════════╗")
    log("║  AUGUR REPORTER — @Sammuraiibot  ║")
    log("╚══════════════════════════════════╝")
    log(f"Model: {MODEL} | Bot: {BOT_TOKEN[:20]}... | Chat: {CHAT_ID}")
    log("Polling...")

    try:
        req.post(f"{TG_API}/deleteWebhook", timeout=10)
        log("[init] webhook cleared")
    except Exception as e:
        log(f"[init] {e}")

    offset = 0
    hot_poll_counter = 0

    while True:
        try:
            now  = time.gmtime()
            yday = now.tm_yday
            slot = (now.tm_hour // 6) * 6

            # 6h report
            if slot != _last_slot:
                _last_slot = slot
                def _send_report():
                    with _report_lock:
                        try:
                            r = generate_report()
                            tg(CHAT_ID, f"📊 *AUGUR 6H REPORT — {time.strftime('%H:%M UTC', now)}*\n\n{r}")
                        except Exception as e:
                            log(f"[6h report error] {e}")
                threading.Thread(target=_send_report, daemon=True, name="6h-report").start()

            # Morning brief 08:00 UTC
            if now.tm_hour == 8 and yday != _last_morning:
                _last_morning = yday
                def _morning():
                    with _report_lock:
                        try:
                            r = generate_report()
                            tg(CHAT_ID, f"🌅 *AUGUR MORNING BRIEF*\n\n{r}")
                        except Exception as e:
                            log(f"[morning error] {e}")
                threading.Thread(target=_morning, daemon=True, name="morning").start()

            # EOD 21:00 UTC
            if now.tm_hour == 21 and yday != _last_eod:
                _last_eod = yday
                def _eod():
                    with _report_lock:
                        try:
                            r = generate_report()
                            tg(CHAT_ID, f"🌙 *AUGUR EOD — {time.strftime('%d %b %Y', now)}*\n\n{r}")
                        except Exception as e:
                            log(f"[eod error] {e}")
                threading.Thread(target=_eod, daemon=True, name="eod").start()

            # Poll hot signals every ~2 min (every 12 iterations @ 10s poll)
            hot_poll_counter += 1
            if hot_poll_counter >= 12:
                hot_poll_counter = 0
                threading.Thread(target=_poll_hot_signals, daemon=True, name="hot-poll").start()

            # Poll Telegram
            resp    = req.get(f"{TG_API}/getUpdates",
                              params={"offset": offset, "timeout": 10},
                              timeout=15)
            updates = resp.json().get("result", [])

            for update in updates:
                offset  = update["update_id"] + 1
                msg     = update.get("message") or update.get("edited_message")
                if not msg:
                    continue
                chat_id = str(msg["chat"]["id"])
                text    = msg.get("text", "").strip()
                if text:
                    log(f"[recv] {chat_id}: {text[:60]}")
                    threading.Thread(
                        target=handle_message,
                        args=(chat_id, text),
                        daemon=True,
                        name=f"msg-{chat_id}"
                    ).start()

        except req.exceptions.Timeout:
            continue
        except KeyboardInterrupt:
            log("Stopped.")
            sys.exit(0)
        except Exception as e:
            log(f"[poll error] {e}\n{traceback.format_exc()}")
            time.sleep(3)


if __name__ == "__main__":
    main()
