# 容器化 Harness 工具运行器示例（v0.24.0）
#
# 构建：docker build -t lc-tool-runner -f examples/contrib/harness/Dockerfile .
# 运行：由 docker_harness.py 自动以 docker run --rm 方式启动

FROM python:3.12-slim

WORKDIR /runner

# 示例：安装 sqlite3 等工具级依赖，但不安装 loop-controller
RUN apt-get update && apt-get install -y --no-install-recommends sqlite3 \
    && rm -rf /var/lib/apt/lists/*

COPY runner.py /runner/runner.py

ENTRYPOINT ["python", "/runner/runner.py"]
