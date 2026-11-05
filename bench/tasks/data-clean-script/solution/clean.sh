#!/usr/bin/env bash
# 先去首尾空白，再按内容过滤；awk 的 seen 负责保序去重
awk '
{
    sub(/^[[:space:]]+/, "")
    sub(/[[:space:]]+$/, "")
    if ($0 == "" || substr($0, 1, 1) == "#" || seen[$0]++) next
    print
}'
