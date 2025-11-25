
from typing import TypeVar, Generic, Type

T = TypeVar('T')


class SuccessValue(Generic[T]):
    def __init__(self, success: bool, value: T, valueType: Type[T] = None) -> None:
        self.success = success
        self.value = value
        self.valueType = valueType or type(value)


# Usage
# s = SuccessValue[str](True, 'hello')
# print(s.value_type)  # <class 'str'>

# class SuccessValue:
#    def __init__(self, success: bool, value):
#        self.success = success
#        self.value = value
