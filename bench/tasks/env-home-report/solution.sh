set -eu
: "${BENCH_HOME:?}"
mkdir -p "$BENCH_HOME/reports"
awk '
{ sum[$1] += $2; total += $2 }
END {
    for (region in sum) if (sum[region] > best) { best = sum[region]; top = region }
    print "lines=" NR
    print "total=" total
    print "top=" top
}' data.txt > "$BENCH_HOME/reports/summary.txt"
