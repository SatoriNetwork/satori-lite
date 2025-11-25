class TwoWayDictionary(dict):
    '''
    a dictionary that can be accessed by key or value since values are unique.

    # Example usage:
    two_way_dict = TwoWayDictionary()
    two_way_dict["apple"] = "red"
    two_way_dict["banana"] = "yellow"

    print(two_way_dict.get_by_key("apple"))  # Output: red
    print(two_way_dict.get_by_value("yellow"))  # Output: banana

    # Reassigning 'apple' to 'green'
    two_way_dict['apple'] = 'green'
    print(two_way_dict.get_by_key("apple"))  # Output: green
    print(two_way_dict.get_by_value("red"))  # Output: None
    print(two_way_dict.get_by_value("green"))  # Output: apple

    print(two_way_dict)
    # Output: Key to Value: {'apple': 'green', 'banana': 'yellow'}
    #         Value to Key: {'green': 'apple', 'yellow': 'banana'}

    # Adding a new key-value pair with an already existing value
    try:
        two_way_dict['weed'] = 'green'
    except ValueError as e:
        print(e)
        # Output: Value 'green' is already associated with key 'apple'
    '''

    def __init__(self):
        self._reverse_dict = {}

    def __setitem__(self, key, value):
        if key in self:
            if self[key] != value:
                # Reassigning the key to a new value, remove the old value from the reverse dictionary
                del self._reverse_dict[self[key]]
        else:
            if value in self._reverse_dict:
                raise ValueError(
                    f"Value '{value}' is already associated with key '{self._reverse_dict[value]}'")
        if value in self._reverse_dict and self._reverse_dict[value] != key:
            raise ValueError(
                f"Key '{value}' is already associated with value '{self._reverse_dict[value]}'")
        super().__setitem__(key, value)
        self._reverse_dict[value] = key

    def __delitem__(self, key):
        value = self[key]
        super().__delitem__(key)
        del self._reverse_dict[value]

    def get_by_key(self, key):
        return self.get(key)

    def get_by_value(self, value):
        return self._reverse_dict.get(value)

    def delete_by_key(self, key):
        if key in self:
            del self[key]

    def delete_by_value(self, value):
        if value in self._reverse_dict:
            del self._reverse_dict[value]

    def __str__(self):
        return f"Key to Value: {dict(self)}\nValue to Key: {self._reverse_dict}"

    @classmethod
    def fromDict(cls, input_dict):
        '''
        # Dictionary conversion
        x = {'apple': 'red', 'banana': 'yellow'}
        try:
            x = TwoWayDictionary.fromDict(x)
            print(x)
            # Output: Key to Value: {'apple': 'red', 'banana': 'yellow'}
            #         Value to Key: {'red': 'apple', 'yellow': 'banana'}
        except ValueError as e:
            print(e)

        # Attempt to convert a dictionary with non-unique values
        x_with_duplicate_values = {'apple': 'red', 'grape': 'red'}
        try:
            x = TwoWayDictionary.fromDict(x_with_duplicate_values)
            print(x)
        except ValueError as e:
            print(e)
            # Output: Values in the input dictionary must be unique  
            # 
        '''
        # Check if values in the input dictionary are unique
        if len(input_dict.values()) != len(set(input_dict.values())):
            raise ValueError("Values in the input dictionary must be unique")

        # Create a new TwoWayDictionary object and populate it with items from the input dictionary
        two_way_dict = cls()
        for key, value in input_dict.items():
            two_way_dict[key] = value

        return two_way_dict
