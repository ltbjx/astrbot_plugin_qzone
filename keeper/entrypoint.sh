#!/bin/sh
# 容器入口：先起一个虚拟显示（Xvfb），再启动保活程序。
#
# 为什么不直接用 xvfb-run？
#   1) xvfb-run 依赖 xauth / xdpyinfo 等工具，精简镜像里往往没有，会卡在等 X 就绪
#   2) 它会吞掉子进程的日志，出问题很难查
# 自己起 Xvfb 更可控，且 python 直接作为 PID 1 接收信号。
set -e

DISPLAY_NUM="${DISPLAY_NUM:-99}"
export DISPLAY=":${DISPLAY_NUM}"

# 容器 restart 后 /tmp 是保留的，陈旧的 X 锁文件会让新 Xvfb 拒绝启动
# （报 "Server is already active for display 99"），所以先清掉
rm -f "/tmp/.X${DISPLAY_NUM}-lock" "/tmp/.X11-unix/X${DISPLAY_NUM}" 2>/dev/null || true

echo "[entrypoint] 启动 Xvfb ${DISPLAY} (1280x900x24)"
Xvfb "${DISPLAY}" -screen 0 1280x900x24 -nolisten tcp &
XVFB_PID=$!

# 等 X socket 就绪（最多 15 秒）
i=0
while [ "$i" -lt 30 ]; do
    if [ -S "/tmp/.X11-unix/X${DISPLAY_NUM}" ]; then
        echo "[entrypoint] X 已就绪"
        break
    fi
    i=$((i + 1))
    sleep 0.5
done

if [ ! -S "/tmp/.X11-unix/X${DISPLAY_NUM}" ]; then
    echo "[entrypoint] 警告：X 未就绪，继续尝试启动（可能失败）" >&2
fi

# Xvfb 挂了要跟着退出，交给 restart: always 重启
trap 'kill "$XVFB_PID" 2>/dev/null; exit 0' TERM INT

# 清理上次异常退出留下的 Chromium 单例锁。
# 否则会报 "The profile appears to be in use by another Chromium process"。
# 本服务同一时间只允许一个实例运行（compose 里就是单容器），所以放心清。
PROFILE_DIR="${PROFILE_DIR:-${STATE_DIR:-/data}/profile}"
if [ -d "$PROFILE_DIR" ]; then
    rm -f "$PROFILE_DIR/SingletonLock" \
          "$PROFILE_DIR/SingletonCookie" \
          "$PROFILE_DIR/SingletonSocket" 2>/dev/null || true
    echo "[entrypoint] 已清理 profile 单例锁：${PROFILE_DIR}"
fi

echo "[entrypoint] 启动 keeper"
exec python3 -u /app/keeper.py
