def paginate(items, page, size):
    if page < 1 or size < 1:
        raise ValueError("page 和 size 都从 1 开始")
    start = (page - 1) * size
    return items[start:start + size]
