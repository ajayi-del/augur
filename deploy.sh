#!/bin/bash
# AUGUR full deploy — run once after git pull
# Usage: cd ~/AUGUR && bash deploy.sh
set -e
echo "═══════════════════════════════════"
echo "  AUGUR DEPLOY"
echo "═══════════════════════════════════"

# ── Step 1: Patch kimi_telegram.py ──────────────────────────────────────────
echo "[1/4] Patching OPS bot (Kimi K2.6 primary, max_steps=25)..."
python3 << 'PYEOF'
path = "/home/dayodapper/kimi_telegram.py"
with open(path) as f:
    lines = f.readlines()

new_lines = []
in_active_client = False
moonshot_rl_added = False

for i, line in enumerate(lines):
    # Track which function we're in
    if 'def _active_client():' in line:
        in_active_client = True
    elif line.startswith('def ') and 'def _active_client' not in line and in_active_client:
        in_active_client = False

    # max_steps 10→25 deep, 5→10 fast
    if 'max_steps = 10 if' in line:
        line = line.replace('max_steps = 10 if', 'max_steps = 25 if').replace('else 5', 'else 10')

    # model/provider lines
    if 'MODEL_AGENT  = "deepseek-chat"' in line:
        line = line.replace('"deepseek-chat"', '"kimi-k2.6"')
    if 'MODEL_REPORT = "deepseek-chat"' in line:
        line = line.replace('"deepseek-chat"', '"kimi-k2.6"')
    if '_provider    = "deepseek"' in line:
        line = line.replace('"deepseek"', '"moonshot"')
    if line.strip() == 'client       = _deepseek_client':
        line = line.replace('client       = _deepseek_client', 'client       = _moonshot_client or _deepseek_client')

    # inject _moonshot_rate_limited_until next to deepseek one
    if '_deepseek_rate_limited_until = 0   # epoch' in line and not moonshot_rl_added:
        new_lines.append('_moonshot_rate_limited_until = 0   # epoch seconds; 0 = not rate limited\n')
        moonshot_rl_added = True

    # _active_client body swap
    if in_active_client:
        if '_deepseek_rate_limited_until > time.time() and _moonshot_client' in line:
            line = line.replace(
                '_deepseek_rate_limited_until > time.time() and _moonshot_client',
                '_moonshot_rate_limited_until > time.time() or _moonshot_client is None')
        elif 'return _moonshot_client, "kimi-k2.6"' in line:
            line = line.replace('return _moonshot_client, "kimi-k2.6"',
                                'return _deepseek_client, "deepseek-chat"')
        elif 'return _deepseek_client, MODEL_REPORT' in line:
            line = line.replace('return _deepseek_client, MODEL_REPORT',
                                'return _moonshot_client, "kimi-k2.6"')

    # run_agent rate-limit: flip to Kimi-limits-out→DeepSeek
    if '"[rate-limit] agent: DeepSeek limited' in line:
        line = line.replace(
            'DeepSeek limited, switching to Kimi K2 for 30min',
            'Kimi rate-limited, switching to DeepSeek for 30min')
    if '"_DeepSeek rate-limited. Switching to Kimi K2 for this session._"' in line:
        line = line.replace(
            '_DeepSeek rate-limited. Switching to Kimi K2 for this session._',
            '_Kimi K2 rate-limited. Switching to DeepSeek for 30min._')
    if '_deepseek_rate_limited_until = time.time() + 1800' in line and 'agent' in ''.join(lines[max(0,i-4):i]):
        line = line.replace('_deepseek_rate_limited_until', '_moonshot_rate_limited_until')

    new_lines.append(line)

with open(path, 'w') as f:
    f.writelines(new_lines)

results = {
    'max_steps=25':       any('max_steps = 25' in l for l in new_lines),
    'kimi_primary':       any('MODEL_AGENT  = "kimi-k2.6"' in l for l in new_lines),
    'moonshot_provider':  any('_provider    = "moonshot"' in l for l in new_lines),
    'moonshot_rl_var':    any('_moonshot_rate_limited_until = 0' in l for l in new_lines),
}
all_ok = all(results.values())
for k, v in results.items():
    print(f"  {'✓' if v else '✗ FAIL'} {k}")
if not all_ok:
    raise SystemExit("Patch incomplete — check above")
PYEOF
echo "    OPS bot patched"

# ── Step 2: Copy reporter bot ────────────────────────────────────────────────
echo "[2/4] Installing AUGUR reporter bot..."
cp ~/AUGUR/augur_reporter_bot.py ~/augur_reporter_bot.py
echo "    Reporter bot installed at ~/augur_reporter_bot.py"

# ── Step 3: Restart AUGUR ────────────────────────────────────────────────────
echo "[3/4] Restarting AUGUR..."
if tmux has-session -t augur 2>/dev/null; then
    tmux send-keys -t augur C-c
    sleep 3
    tmux send-keys -t augur 'cd ~/AUGUR && .venv/bin/python3 main.py' Enter
    echo "    AUGUR restarted in tmux:augur"
else
    echo "    WARN: tmux session 'augur' not found — start manually:"
    echo "    tmux new-session -d -s augur 'cd ~/AUGUR && .venv/bin/python3 main.py'"
fi

# ── Step 4: Restart bots ─────────────────────────────────────────────────────
echo "[4/4] Restarting Telegram bots..."

# OPS bot
pkill -f kimi_telegram.py 2>/dev/null && sleep 1 || true
nohup python3 ~/kimi_telegram.py >> ~/kimi_telegram.log 2>&1 &
OPS_PID=$!
echo "    OPS bot PID: $OPS_PID"

# Reporter bot
if tmux has-session -t augur-reporter 2>/dev/null; then
    tmux send-keys -t augur-reporter C-c
    sleep 1
    tmux send-keys -t augur-reporter 'python3 ~/augur_reporter_bot.py' Enter
else
    tmux new-session -d -s augur-reporter 'python3 ~/augur_reporter_bot.py 2>&1 | tee ~/augur_reporter.log'
fi
echo "    Reporter bot running in tmux:augur-reporter"

# ── Verify ───────────────────────────────────────────────────────────────────
echo ""
echo "═══ VERIFYING (10s) ═══"
sleep 10
echo ""
echo "── OPS bot log:"
tail -4 ~/kimi_telegram.log 2>/dev/null || echo "(no log yet)"
echo ""
echo "── AUGUR log:"
tmux capture-pane -t augur -p 2>/dev/null | tail -4 || echo "(check tmux)"
echo ""
echo "── Reporter bot:"
tmux capture-pane -t augur-reporter -p 2>/dev/null | tail -3 || echo "(check tmux)"
echo ""
echo "═══ DEPLOY COMPLETE ═══"
echo "Monitor:"
echo "  OPS:      tail -f ~/kimi_telegram.log"
echo "  Reporter: tmux attach -t augur-reporter"
echo "  AUGUR:    tmux attach -t augur"

# Clean up deploy key
sed -i '/augur_deploy_tmp/d' ~/.ssh/authorized_keys 2>/dev/null && echo "  (temp SSH key removed)" || true
