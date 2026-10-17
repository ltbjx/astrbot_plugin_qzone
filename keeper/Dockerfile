FROM python:3.11-slim

ENV DEBIAN_FRONTEND=noninteractive \
    TZ=Asia/Shanghai \
    PYTHONUNBUFFERED=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

# 中文字体（网页渲染 + 截图需要）、Xvfb（headful 模式需要）、xauth（xvfb-run 需要）
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates xvfb xauth fonts-noto-cjk fonts-noto-color-emoji \
    && rm -rf /var/lib/apt/lists/*

# Playwright（国内走清华源）
RUN pip install --no-cache-dir -i https://pypi.tuna.tsinghua.edu.cn/simple playwright

# Chromium 及其系统依赖
#   国内网络拉不动 cdn.playwright.dev 时，取消下面这行的注释改用 npmmirror 镜像
# ENV PLAYWRIGHT_DOWNLOAD_HOST=https://cdn.npmmirror.com/binaries/playwright
RUN playwright install --with-deps chromium

WORKDIR /app
COPY keeper.py /app/keeper.py
COPY panel.py /app/panel.py
COPY dashboard.html /app/dashboard.html
COPY login.html /app/login.html
COPY entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

EXPOSE 6186
VOLUME ["/data", "/out"]

# 自己起 Xvfb（不用 xvfb-run，避免它对 xauth/xdpyinfo 的依赖和吞日志问题）
CMD ["/app/entrypoint.sh"]
