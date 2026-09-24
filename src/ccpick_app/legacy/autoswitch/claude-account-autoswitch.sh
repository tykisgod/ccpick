#!/bin/bash
set -uo pipefail

if [ -z "${HOME:-}" ]; then
  HOME=$(/usr/bin/dscl . -read "/Users/$(/usr/bin/id -un)" NFSHomeDirectory 2>/dev/null \
         | /usr/bin/awk '{print $2}')
  [ -n "$HOME" ] || HOME="/Users/$(/usr/bin/id -un)"
  export HOME
fi

LOG="$HOME/Library/Logs/claude-account-autoswitch.log"
NOTIFIED="$HOME/Library/Logs/.claude-autoswitch-notified"
STATUS="$HOME/Library/Logs/claude-autoswitch-status.json"
LOCKDIR="$HOME/Library/Logs/.claude-autoswitch.lock"
HELPER_ERR="$HOME/Library/Logs/claude-autoswitch-child.stderr.log"
DECIDE="$HOME/bin/claude-autoswitch-decide.py"
HELPER="$HOME/bin/claude-autoswitch-helper.py"

log() { printf '%s  %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" >> "$LOG"; }
notify() { /usr/bin/osascript -e "display notification \"$2\" with title \"$1\" sound name \"Submarine\"" >/dev/null 2>&1; }

if [ -f "$LOG" ] && [ "$(wc -c < "$LOG" 2>/dev/null || echo 0)" -gt 2000000 ]; then
  tail -c 500000 "$LOG" > "$LOG.tmp" 2>/dev/null && mv "$LOG.tmp" "$LOG"
fi

[ -f "$DECIDE" ] || { log "FAIL 找不到决策脚本 $DECIDE"; exit 1; }

_take_lock() {
  mkdir "$LOCKDIR" 2>/dev/null || return 1
  printf '%s' "$$" > "$LOCKDIR/pid"
  return 0
}

if ! _take_lock; then
  holder=$(cat "$LOCKDIR/pid" 2>/dev/null || echo "")
  if [ -n "$holder" ] && kill -0 "$holder" 2>/dev/null; then
    log "SKIP 另一轮正在跑 (pid=$holder), 本轮让路"; exit 0
  fi
  if [ -z "$holder" ]; then
    born=$(stat -f %m "$LOCKDIR" 2>/dev/null)
    case "$born" in ''|*[!0-9]*) born=$(stat -c %Y "$LOCKDIR" 2>/dev/null);; esac
    case "$born" in ''|*[!0-9]*) born=0;; esac
    now=$(date +%s)
    if [ "$born" -gt 0 ] && [ $((now - born)) -lt 10 ]; then
      log "SKIP 锁刚建且还没写 pid, 可能对方正在写, 本轮让路"; exit 0
    fi
  fi
  log "回收陈旧锁 (pid=${holder:-未知} 已不在)"
  rm -rf "$LOCKDIR" 2>/dev/null
  _take_lock || { log "SKIP 抢不到锁"; exit 0; }
fi
trap 'rm -rf "$LOCKDIR" 2>/dev/null' EXIT INT TERM

if ! /usr/bin/curl -s -o /dev/null --max-time 8 https://claude.ai/ 2>/dev/null; then
  log "SKIP 连不上 claude.ai (断网/睡醒瞬间)"
  /usr/bin/python3 "$HELPER" write "$STATUS" offline "连不上 claude.ai" "下一轮自己重试" "${CCSWITCH_THRESHOLD:-90}" "" "" 2>>"$HELPER_ERR"
  exit 0
fi

out=$(/usr/bin/python3 "$DECIDE" 2>&1)
rc=$?
act=$(printf '%s' "$out" | /usr/bin/python3 -c "
import json,sys
try: print(json.loads(sys.stdin.read()).get('action',''))
except Exception: print('')
" 2>/dev/null)

wins=$(printf '%s' "$out" | /usr/bin/python3 -c "
import json,sys
try: o=json.loads(sys.stdin.read())
except Exception: o={}
w=o.get('windows') or {}
u=o.get('used') if o.get('used') is not None else o.get('toUsed')
def g(k):
    v=w.get(k)
    return '-' if v is None else str(int(round(float(v))))
model=next((k for k in w if k not in ('5h','7d')), None)
cw=o.get('countedWindows')
mc='' if (cw is None or not model) else ('1' if model in cw else '0')
clean=lambda s: (s or '').replace('|', '')
print('%s|%s|%s|%s|0|%s|%s|%s' % ('-' if u is None else int(round(float(u))), g('5h'), g('7d'),
      g(model) if model else '-', clean(o.get('binding')), clean(model), mc))
" 2>/dev/null)

sched=$(printf '%s' "$out" | /usr/bin/python3 -c "
import json,sys
try: o=json.loads(sys.stdin.read())
except Exception: o={}
def g(k):
    try: return '%.1f' % float(o.get(k))
    except (TypeError, ValueError): return ''
print('%s|%s|%s|%s' % (g('nextCheckS'), g('etaS'), g('burnRate'),
                       (o.get('activeEmail') or '').replace('|', '')))
" 2>/dev/null)

thr=$(printf '%s' "$out" | /usr/bin/python3 -c "
import json,sys
try: v=json.loads(sys.stdin.read()).get('consider')
except Exception: v=None
print('' if v is None else '%.0f' % float(v))
" 2>/dev/null)
[ -n "$thr" ] || thr="${CCSWITCH_THRESHOLD:-90}"
extra=$(printf '%s' "$out" | /usr/bin/python3 -c "
import json,sys
try: o=json.loads(sys.stdin.read())
except Exception: o={}
r, e, c = o.get('burnRate'), o.get('etaS'), o.get('consider') or 90
try:
    print('烧 %.0f 点/分, 约 %d 分钟到顶' % (float(r), max(1, round(float(e)/60))))
except (TypeError, ValueError):
    print('到 %.0f%% 或快见底就切' % float(c))
" 2>/dev/null)

case "$rc" in
  0)
    to=$(printf '%s' "$out" | /usr/bin/python3 -c "import json,sys;print(json.loads(sys.stdin.read()).get('to',''))" 2>/dev/null)
    log "SWITCHED $out"
    notify "Claude 账号已自动切换" "现在：${to:-见日志}"
    /usr/bin/python3 "$HELPER" write "$STATUS" switched "刚切到 ${to:-?}" "刚落地, 先紧盯几轮" "$thr" "$wins" "$sched" 2>>"$HELPER_ERR"
    rm -f "$NOTIFIED"
    ;;
  2)
    sfail=$(printf '%s' "$out" | /usr/bin/python3 -c "
import json,sys
try: o=json.loads(sys.stdin.read())
except Exception: o={}
print((o.get('why') or '切换没成功')[:120] if o.get('switchFailed') else '')
" 2>/dev/null)
    if [ -n "$sfail" ]; then
      log "WARN 切换失败, 留在原号 $out"
      /usr/bin/python3 "$HELPER" write "$STATUS" ok "切换没成功, 先用着当前号" "$sfail" "$thr" "$wins" "$sched" 2>>"$HELPER_ERR"
      now=$(date +%s); last=$(cat "$NOTIFIED.switchfail" 2>/dev/null)
      case "$last" in ''|*[!0-9]*) last=0;; esac
      if [ $((now - last)) -ge 3600 ]; then
        notify "Claude 自动切号没切成" "当前号还能用; 看日志 ~/Library/Logs/claude-account-autoswitch.log"
        printf '%s' "$now" > "$NOTIFIED.switchfail"
      fi
    else
      log "ok 无需切换 $out"
      /usr/bin/python3 "$HELPER" write "$STATUS" ok "在盯着" "$extra" "$thr" "$wins" "$sched" 2>>"$HELPER_ERR"
      rm -f "$NOTIFIED.switchfail"
    fi
    rm -f "$NOTIFIED"
    ;;
  3)
    soon=$(printf '%s' "$out" | /usr/bin/python3 -c "
import json,sys,datetime
try: o=json.loads(sys.stdin.read())
except Exception: o={}
a=o.get('soonest') or ''
t=''
iso=o.get('soonestAt') or ''
if iso:
    try:
        lt=datetime.datetime.fromisoformat(iso.replace('Z','+00:00')).astimezone()
        days=(lt.date()-datetime.datetime.now().astimezone().date()).days
        t=lt.strftime('今天 %H:%M') if days==0 else (
          lt.strftime('明天 %H:%M') if days==1 else lt.strftime('%m-%d %H:%M'))
    except Exception:
        t=''
print(('%s 最早恢复 %s' % (a, t)) if (a and t) else '等最早那个恢复')
" 2>/dev/null)
    log "★BLOCKED 没有可切的账号。$soon | $out"
    /usr/bin/python3 "$HELPER" write "$STATUS" blocked "全部账号见底" "$soon" "$thr" "$wins" "$sched" 2>>"$HELPER_ERR"
    now=$(date +%s); last=$(cat "$NOTIFIED" 2>/dev/null || echo 0)
    if [ $((now - last)) -gt 3600 ]; then
      notify "Claude 全部账号额度用尽" "$soon"
      printf '%s' "$now" > "$NOTIFIED"
    fi
    ;;
  *)
    log "ERROR rc=$rc $out"
    errmsg=$(printf '%s' "$out" | /usr/bin/python3 -c "
import sys
lines = sys.stdin.read().strip().splitlines()
print((lines[-1] if lines else '')[:120])
" 2>/dev/null)
    /usr/bin/python3 "$HELPER" write "$STATUS" error "决策出错" "$errmsg" "${CCSWITCH_THRESHOLD:-90}" "" "" 2>>"$HELPER_ERR"
    ;;
esac
exit 0
