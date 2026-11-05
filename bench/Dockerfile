# 基准的一次性运行环境。构建上下文是 git archive 的结果，所以只有提交过的文件，.env 和本机路径都进不来。
# 用 bench/run.sh 构建和运行。不挂任何宿主目录，结果用 docker cp 取走。
FROM python:3.12-slim-trixie

RUN apt-get update -qq \
    && apt-get install -y -qq --no-install-recommends bubblewrap git curl ca-certificates procps >/dev/null \
    && pip install -q uv==0.10.10 \
    && useradd -m bench \
    && mkdir -p /results /work \
    && chown bench /results /work

USER bench
WORKDIR /home/bench/repo
COPY --chown=bench . .

# BENCH_SPEC 为空时测这个提交的源码；写 flywhl-harness==0.2.0 时测 PyPI 上发布的版本，别人复现用这一种
ARG BENCH_SPEC=""
ARG BENCH_COMMIT=""
RUN uv sync --frozen --extra anthropic --group dev -q \
    && if [ -n "$BENCH_SPEC" ]; then uv pip install -q --no-sources "$BENCH_SPEC"; fi

ENV FLYWHL_BENCH_DISPOSABLE=1 BENCH_SPEC=$BENCH_SPEC BENCH_COMMIT=$BENCH_COMMIT BENCH_WORK=/work PATH=/home/bench/repo/.venv/bin:$PATH
ENTRYPOINT ["python", "-m", "bench"]
CMD ["all"]
