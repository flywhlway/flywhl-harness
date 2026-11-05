set -eu
python3 -m venv .venv
.venv/bin/python -m pip install --quiet --no-index --find-links wheels hello_bench
.venv/bin/hello-bench > out.txt
