STOCK = {"book": 10, "pen": 100}


def validate(item_id, quantity):
    if not isinstance(item_id, str) or not item_id.strip():
        raise ValueError("item_id must be a nonempty string")
    if type(quantity) is not int or not 1 <= quantity <= 2_147_483_647:
        raise ValueError("quantity must be a positive int32 integer")


def available(item_id, quantity):
    return STOCK.get(item_id, 0) >= quantity
