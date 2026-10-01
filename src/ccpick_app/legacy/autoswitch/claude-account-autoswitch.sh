#!/bin/bash
set -uo pipefail

if [ -z "${HOME:-}" ]; then
  HOME=$(/usr/bin/dscl . -read "/Users/$(/usr/bin/id -un)" NFSHomeDirectory 2>/dev/null \
         | /usr/bin/awk '{print $2}')
  [ -n "$HOME" ] || HOME="/Users/$(/usr/bin/id -un)"
  export HOME
fi

FIXED_HOUSE_PROXY='http://127.0.0.1:11808'
export HTTP_PROXY="$FIXED_HOUSE_PROXY" HTTPS_PROXY="$FIXED_HOUSE_PROXY" ALL_PROXY="$FIXED_HOUSE_PROXY"
export http_proxy="$FIXED_HOUSE_PROXY" https_proxy="$FIXED_HOUSE_PROXY" all_proxy="$FIXED_HOUSE_PROXY"
export NO_PROXY='localhost,127.0.0.1,::1' no_proxy='localhost,127.0.0.1,::1'

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

if [ "${1:-}" != "--force-check" ] && /usr/bin/python3 "$HELPER" wait "$STATUS" 2>>"$HELPER_ERR"; then
  exit 0
fi

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
    dnotice=$(printf '%s' "$out" | /usr/bin/python3 -c "
import json,sys
try: o=json.loads(sys.stdin.read())
except Exception: o={}
d=o.get('deniedFrom') or ''
print(((o.get('notice') or '%s 被拒 (403), 疑似被封; 现在: %s' % (d, o.get('to') or '?'))[:120]) if d else '')
" 2>/dev/null)
    if [ -n "$dnotice" ]; then
      log "★SWITCHED 原号疑似被停用 (用量接口 403) $out"
      notify "Claude 账号疑似被封, 已自动换号" "$dnotice"
      /usr/bin/python3 "$HELPER" write "$STATUS" switched "刚切到 ${to:-?}" "$dnotice" "$thr" "$wins" "$sched" 2>>"$HELPER_ERR"
    else
      log "SWITCHED $out"
      notify "Claude 账号已自动切换" "现在：${to:-见日志}"
      /usr/bin/python3 "$HELPER" write "$STATUS" switched "刚切到 ${to:-?}" "刚落地, 先紧盯几轮" "$thr" "$wins" "$sched" 2>>"$HELPER_ERR"
    fi
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
      wo=$(printf '%s' "$out" | /usr/bin/python3 -c "
import json,sys
try: o=json.loads(sys.stdin.read())
except Exception: o={}
print(('W|' + (o.get('wouldSwitchTo') or '').replace('|', '')) if o.get('watchOnly') else '')
" 2>/dev/null)
      wto=${wo#W|}
      wnote=$(printf '%s' "$out" | /usr/bin/python3 -c "
import json,sys
try: o=json.loads(sys.stdin.read())
except Exception: o={}
print((o.get('notice') or '').replace(chr(10), ' ')[:120] if o.get('currentDenied') else '')
" 2>/dev/null)
      if [ -n "$wo" ] && [ -n "$wto" ] && [ -n "$wnote" ]; then
        log "WATCH 当前号疑似被封, 只看不切, 建议 $wto $out"
        /usr/bin/python3 "$HELPER" write "$STATUS" advise "该换号了: 建议 $wto" "$wnote" "$thr" "$wins" "$sched" 2>>"$HELPER_ERR"
        now=$(date +%s); last=$(cat "$NOTIFIED.denied" 2>/dev/null)
        case "$last" in ''|*[!0-9]*) last=0;; esac
        if [ $((now - last)) -ge 3600 ]; then
          notify "Claude 账号疑似被封, 请手动换号" "$wnote"
          printf '%s' "$now" > "$NOTIFIED.denied"
        fi
      elif [ -n "$wo" ] && [ -n "$wto" ]; then
        log "WATCH 该换号了但只看不切, 建议 $wto $out"
        /usr/bin/python3 "$HELPER" write "$STATUS" advise "该换号了: 建议 $wto" "自动切换已关; 在下面点账号名即可手动切" "$thr" "$wins" "$sched" 2>>"$HELPER_ERR"
        now=$(date +%s); last=$(cat "$NOTIFIED.watch" 2>/dev/null)
        case "$last" in ''|*[!0-9]*) last=0;; esac
        if [ $((now - last)) -ge 3600 ]; then
          notify "Claude 该换号了" "自动切换已关; 建议切到 $wto"
          printf '%s' "$now" > "$NOTIFIED.watch"
        fi
      elif [ -n "$wo" ]; then
        log "ok 只看不切, 无需换号 $out"
        case "$extra" in 到*) wextra="自动切换已关; 该换号时会提醒";; *) wextra="$extra · 自动切换已关";; esac
        /usr/bin/python3 "$HELPER" write "$STATUS" ok "只看不切" "$wextra" "$thr" "$wins" "$sched" 2>>"$HELPER_ERR"
      else
        log "ok 无需切换 $out"
        /usr/bin/python3 "$HELPER" write "$STATUS" ok "在盯着" "$extra" "$thr" "$wins" "$sched" 2>>"$HELPER_ERR"
      fi
      rm -f "$NOTIFIED.switchfail"
      [ -n "$wnote" ] || rm -f "$NOTIFIED.denied"
    fi
    rm -f "$NOTIFIED"
    ;;
  3)
    sfail=$(printf '%s' "$out" | /usr/bin/python3 -c "
import json,sys
try: o=json.loads(sys.stdin.read())
except Exception: o={}
print((o.get('why') or '切换没成功')[:120] if o.get('switchFailed') else '')
" 2>/dev/null)
    dinfo=$(printf '%s' "$out" | /usr/bin/python3 -c "
import json,sys
try: o=json.loads(sys.stdin.read())
except Exception: o={}
if o.get('activeDenied') or o.get('deniedFrom'):
    msg = ('当前号疑似被封, 还在确认' if o.get('denyUnsure') else
           '原号疑似被封, 换到的也满了' if not o.get('activeDenied') else
           '当前号疑似被封, 切换没成功' if o.get('switchFailed') else '当前号疑似被封, 没得换')
    txt = (o.get('notice') or o.get('why') or '当前号被拒 (403), 疑似被封').replace(chr(10), ' ')[:120]
    print(msg); print(txt); print('1' if o.get('quiet') else '0')
" 2>/dev/null)
    if [ -n "$dinfo" ]; then
      dmsg=$(printf '%s\n' "$dinfo" | sed -n 1p)
      dtxt=$(printf '%s\n' "$dinfo" | sed -n 2p)
      dquiet=$(printf '%s\n' "$dinfo" | sed -n 3p)
      log "★BLOCKED $dmsg $out"
      /usr/bin/python3 "$HELPER" write "$STATUS" blocked "$dmsg" "$dtxt" "$thr" "$wins" "$sched" 2>>"$HELPER_ERR"
      if [ "$dquiet" != "1" ]; then
        now=$(date +%s); last=$(cat "$NOTIFIED.denied" 2>/dev/null)
        case "$last" in ''|*[!0-9]*) last=0;; esac
        if [ $((now - last)) -ge 3600 ]; then
          notify "Claude 账号疑似被封" "$dtxt"
          printf '%s' "$now" > "$NOTIFIED.denied"
        fi
      fi
    elif [ -n "$sfail" ]; then
      log "★BLOCKED 切换失败, 当前号也快满了 $out"
      /usr/bin/python3 "$HELPER" write "$STATUS" blocked "切换没成功, 当前号也快满了" "$sfail" "$thr" "$wins" "$sched" 2>>"$HELPER_ERR"
      now=$(date +%s); last=$(cat "$NOTIFIED.switchfail" 2>/dev/null)
      case "$last" in ''|*[!0-9]*) last=0;; esac
      if [ $((now - last)) -ge 3600 ]; then
        notify "Claude 自动切号没切成" "当前号也快满了; 看日志 ~/Library/Logs/claude-account-autoswitch.log"
        printf '%s' "$now" > "$NOTIFIED.switchfail"
      fi
    else
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
    printf '%s' "$out" | /usr/bin/python3 "$HELPER" defer "$STATUS" 2>>"$HELPER_ERR"
    now=$(date +%s)
    if [ ! -f "$NOTIFIED" ]; then
      notify "Claude 全部账号额度用尽" "$soon"
      printf '%s' "$now" > "$NOTIFIED"
    fi
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
if [ "$rc" -eq 2 ]; then
  printf '%s' "$out" | /usr/bin/python3 "$HELPER" schedule "$STATUS" 2>>"$HELPER_ERR"
fi
exit 0
