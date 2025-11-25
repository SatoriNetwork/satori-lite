from typing import Union


def eq(a: Union[int, float, str, bytes], b: Union[int, float, str, bytes]):
    ''' compares two values and returns True if they are the same '''
    if a == b:
        return True
    if str(a) == str(b):
        return True
    try:
        if float(a) == float(b):
            return True
    except Exception as _:
        pass
    if isinstance(a, str) and isinstance(b, bytes):
        try:
            return float(a) == float(b)
        except Exception as _:
            return bytes(a, 'utf-8') == b
    if isinstance(a, bytes) and isinstance(b, str):
        try:
            return float(a) == float(b)
        except Exception as _:
            return a == bytes(b, 'utf-8')
    return False

# test cases:
# print('Trues:')
# eq('3.14', 3.14)
# eq(.17, '0.17')
# eq(0.17, '.17')
# eq(97, '97.00000')
# eq(b'hello world', 'hello world')
# eq(b'1', '1')
# eq(b'1.0', '1')
# eq(b'1', '1.0')
# eq(b'1', 1)
# eq(b'1', 1.0)
# eq(b'1.0', 1)
# eq(b'1.0100', 1.01)

# print('Falses:')
# eq('3.014', 3.14)
# eq(.17, '.017')
# eq(97, '97.000001')
# eq(b'Hello world', 'hello world')
# eq(b'1', '10')
# eq(b'1', 10)
# eq(b'1.01100', 1.01)
# eq(1.01100, 1.01)
# eq(1.01100, 1)
