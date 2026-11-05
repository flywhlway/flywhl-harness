def paginate(items, page, size):
    start = page * size
    end = start + size
    return items[start:end]
