''' non-async generator coroutine for reference only '''


def simple_coroutine():
    '''
    use:

        # Create the coroutine
        my_coroutine = simple_coroutine()

        # Start the coroutine
        next(my_coroutine)

        # Send a value to the coroutine
        my_coroutine.send(10)

        # Close the coroutine
        my_coroutine.close()

    '''
    print("-> Coroutine started")
    x = yield
    print("-> Coroutine received:", x)
