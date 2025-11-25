def chain(initial_value, *funcs):
    ''' chain merely allows for different syntax for function composition.
        instead of 
        ```result = append_to_list(
            punch(
                bind(k, prefix='>>'), 
                *v, 
                suffix='<<'),
            socks,
            notify=True)```
        you can chain the value through the functions like so:
        ```result = chain(
           k,
           (bind, {'prefix': '>>'}),
           (punch, *v, {'suffix': '<<'}),
           (append_to_list, socks, {'notify': True}))```
        for what it's worth. maybe it's easier to read the chain which is like:
        k.bind.punch.append_to_list if you're into that kind of thing. otherwise
        you have to read it middle-out which I find annoying.
    '''
    value = initial_value
    for func_entry in funcs:
        if isinstance(func_entry, tuple):
            func, *args = func_entry
            kwargs = args.pop() if args and isinstance(args[-1], dict) else {}
            value = func(value, *args, **kwargs)
        else:
            value = func_entry(value)
    return value

# Example usage
# def bind(x, prefix=''):
#    return f"{prefix}bind({x})"
#
# def punch(x, y, z, suffix=''):
#    return f"punch({x}, {y}, {z}){suffix}"
#
# def append_to_list(item, lst, notify=False):
#    lst.append(item)
#    if notify:
#        print(f"Item {item} added to list.")
#    return lst
#
# k = 10
# v = [20, 30]
# socks = []
#
# result = chain(
#    k,
#    (bind, {'prefix': '>>'}),
#    (punch, *v, {'suffix': '<<'}),
#    (append_to_list, socks, {'notify': True})
# )
#
# print("Result:", result)
# print("Socks:", socks)


class Chainable:
    ''' Chainable merely allows for different syntax for function composition.
        instead of 
        ```result = append_to_list(
            punch(
                bind(k, prefix='>>'), 
                *v, 
                suffix='<<'),
            socks,
            notify=True)```
        you can chain the value through the functions like so:
        ```
        result = (
            Chainable(k)
            .then(bind, {'prefix': '>>'})
            .then(punch, *v, {'suffix': '<<'})
            .then(append_to_list, socks, {'notify': True})
        ).value
        ```
        for what it's worth, if you're into that kind of thing. of course, it 
        only works for functions where you're chaining the first argument, so,
        ya know. whatever.
    '''

    def __init__(self, value):
        self.value = value

    def then(self, func, *args, **kwargs):
        if callable(func):
            # If args contains a dict at the end, treat it as kwargs
            if args and isinstance(args[-1], dict):
                kwargs.update(args[-1])
                args = args[:-1]
            self.value = func(self.value, *args, **kwargs)
        return self

# Example usage of the Chainable class
#
# def bind(x, prefix=''):
#    return f"{prefix}bind({x})"
#
#
# def punch(x, y, z, suffix=''):
#    return f"punch({x}, {y}, {z}){suffix}"
#
#
# def append_to_list(item, lst, notify=False):
#    lst.append(item)
#    if notify:
#        print(f"Item {item} added to list.")
#    return lst
#
#
# k = Chainable(10)
# v = [20, 30]
# socks = []
#
# result = (
#    k
#    .then(bind, {'prefix': '>>'})
#    .then(punch, *v, {'suffix': '<<'})
#    .then(append_to_list, socks, {'notify': True})
# ).value
#
# print("Result:", result)
# print("Socks:", socks)


class ComplexChainable:
    ''' this version allows you to chain to any arg or kwarg signature. '''

    def __init__(self, value):
        self.value = value

    def then(self, func, *args, **kwargs):
        if callable(func):
            # Replace placeholder with the current value in args
            args = [
                self.value if arg is ComplexChainable.Value else arg for arg in args]
            # Replace placeholder with the current value in kwargs
            kwargs = {k: (self.value if v is ComplexChainable.Value else v)
                      for k, v in kwargs.items()}
            self.value = func(*args, **kwargs)
        return self

    def v(self):
        return self.Value

    class Value:
        """ Placeholder for the current value. """
        pass

# Example usage
# def bind(x, prefix=''):
#    return f"{prefix}bind({x})"
#
#
# def punch(x, y, z, suffix=''):
#    return f"punch({x}, {y}, {z}){suffix}"
#
#
# def append_to_list(item, lst, notify=False):
#    lst.append(item)
#    if notify:
#        print(f"Item {item} added to list.")
#    return lst
#
#
# k = 10
# v = [20, 30]
# socks = []
#
# result = (
#    ComplexChainable(k)
#    .then(bind, ComplexChainable.v, {'prefix': '>>'})
#    .then(punch, ComplexChainable.v, *v, {'suffix': '<<'})
#    .then(append_to_list, ComplexChainable.v, socks, {'notify': True})
# ).value
#
# print("Result:", result)
# print("Socks:", socks)


class ComplexChainableAsync:
    '''
    this version is async safe, but you have to use the .v() method on the 
    object itself. so you can't use throwaway objects. instead of this:
    ```
    result = (
        ComplexChainable(k)
        .then(bind, ComplexChainable.Value, ...)
        ...
    ).value
    ```
    you have to do this:
    ```
    cca = ComplexChainableAsync(k)
    result = (
        cca.then
        .then(bind, cca.v, ...)
        ...
    ).value
    ```
    '''

    def __init__(self, value):
        self.value = value
        self.v = object()  # Unique placeholder for each instance

    def then(self, func, *args, **kwargs):
        if callable(func):
            # Replace placeholder with the current value in args
            args = [self.value if arg is self.v else arg for arg in args]
            # Replace placeholder with the current value in kwargs
            kwargs = {k: (self.value if v is self.v else v)
                      for k, v in kwargs.items()}
            self.value = func(*args, **kwargs)
        return self

# Example usage
# def bind(x, prefix=''):
#    return f"{prefix}bind({x})"
#
#
# def punch(x, y, z, suffix=''):
#    return f"punch({x}, {y}, {z}){suffix}"
#
#
# def append_to_list(item, lst, notify=False):
#    lst.append(item)
#    if notify:
#        print(f"Item {item} added to list.")
#    return lst
#
#
# k = 10
# v = [20, 30]
# socks = []
#
# cca = ComplexChainableAsync(k)
# result = (
#    cca
#    .then(bind, cca.v, {'prefix': '>>'})
#    .then(punch, cca.v, *v, {'suffix': '<<'})
#    .then(append_to_list, cca.v, socks, {'notify': True})
# ).value
#
# print("Result:", result)
# print("Socks:", socks)
#
