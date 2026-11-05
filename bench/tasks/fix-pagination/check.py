import sys

sys.path.insert(0, sys.argv[1])
from pager import paginate

data = list(range(1, 8))
assert paginate(data, 1, 3) == [1, 2, 3], "第 1 页不对"
assert paginate(data, 3, 3) == [7], "最后一页不对"
assert paginate(data, 4, 3) == [], "超出范围要返回空列表"
for page, size in ((0, 3), (1, 0), (-1, 2)):
    try:
        paginate(data, page, size)
    except ValueError:
        continue
    sys.exit(f"page={page} size={size} 应当抛 ValueError")
