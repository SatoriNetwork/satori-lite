'''
usually we use threads, because we typically have few but in some cases where we
need to scale the number of simple concurrent operations we can use asyncio.
yet the main thread is not run as an asyncio event loop, so we need to set up a
dedicated thread just for that.
'''
import inspect
import asyncio
# todo: integrate stop events into the AsyncThread for graceful shutdown:
# https://chat.openai.com/share/149385bc-e9a4-442a-a973-34d473766f0a

import threading
import datetime as dt
from satorilib import logging
import traceback


class AsyncThread():

    def __init__(self):
        self.loop = None
        self._runForever()

    def startAsyncEventLoop(self):
        ''' runs in a separate thread and maintains the async event loop '''
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def cancelTask(self, future):
        ''' cancels the given asyncio.Future task '''
        if future is not None and not future.done():
            future.cancel()
        # AttributeError: 'function' object has no attribute 'done'

    async def asyncWrapper(self, *args, func: callable, **kwargs):
        try:
            return func(*args, **kwargs)
        except Exception as e:
            # Handle or log the exception as needed
            logging.error(f'Exception in asyncWrapper: {e}', print=True)
            traceback.print_exc()
            raise

    async def delayedWrapper(self, *args, func: callable, delay: float, **kwargs):
        if isinstance(delay, int):
            delay = float(delay)
        if isinstance(delay, float):
            await asyncio.sleep(delay)
        try:
            return func(*args, **kwargs)
        except Exception as e:
            logging.error(f'Exception in delayedWrapper: {e}', print=True)
            raise

    async def repeatWrapper(self, *args, func: callable, interval: float, **kwargs):
        if isinstance(interval, int):
            interval = float(interval)
        while True:
            try:
                await self.asyncWrapper(*args, func=func, **kwargs)
                await asyncio.sleep(interval)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logging.error(f'Exception in repeatWrapper: {e}', print=True)

    async def dailyWrapper(self, *args, func: callable, times: list[str], **kwargs):
        while True:
            now = dt.datetime.utcnow()
            nextRunTime = None
            for timeString in times:
                runTime = (
                    dt.datetime.strptime(timeString, '%H:%M:%S')
                    .replace(year=now.year, month=now.month, day=now.day))
                if runTime < now:
                    runTime += dt.timedelta(days=1)
                if nextRunTime is None or runTime < nextRunTime:
                    nextRunTime = runTime
            delay = (nextRunTime - now).total_seconds()
            await asyncio.sleep(delay)
            try:
                await self.asyncWrapper(*args, func=func, **kwargs)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logging.error(f'Exception in dailyWrapper: {e}', print=True)

    def _preRun(
        self,
        *args,
        task: callable = None,
        delay: float = None,
        interval: float = None,
        times: list[str] = None,
        **kwargs
    ):
        if self.loop is None:
            self._runForever()
        if self.loop is None:
            raise Exception('Event loop is not running.')
        if inspect.iscoroutinefunction(task):
            coroutine = task(*args, **kwargs)
        elif inspect.isfunction(task) or inspect.ismethod(task):
            if delay is not None:
                coroutine = self.delayedWrapper(
                    *args, func=task, delay=delay, **kwargs)
            elif interval is not None:
                coroutine = self.repeatWrapper(
                    *args, func=task, interval=interval, **kwargs)
            elif times is not None:
                coroutine = self.dailyWrapper(
                    *args, func=task, times=times, **kwargs)
            else:
                coroutine = self.asyncWrapper(*args, func=task, **kwargs)
        else:
            raise TypeError('Task must be an async or a regular function.')
        return coroutine

    def runAsync(self, *args, task: callable = None, **kwargs):
        ''' submits async task or function to the event loop '''
        return self._runAsync(self._preRun(*args, task=task, **kwargs))

    def delayedRun(self, *args, task: callable = None, delay: float = 5, **kwargs):
        ''' submits async tasks to the event loop with a delay '''
        return self._runAsync(self._preRun(*args, task=task, delay=delay, **kwargs))

    def repeatRun(self, *args, task: callable, interval: float = 60, **kwargs):
        return self._runAsync(self._preRun(*args, task=task, interval=interval, **kwargs))

    def dailyRun(self, *args, task: callable, times: list[str], **kwargs):
        return self._runAsync(self._preRun(*args, task=task, times=times, **kwargs))

    def _runAsync(self, coroutine: callable):
        ''' submits async task or function to the event loop '''
        return asyncio.run_coroutine_threadsafe(coroutine, self.loop)

    def _runForever(self):
        thread = threading.Thread(target=self.startAsyncEventLoop, daemon=True)
        thread.start()


# from concurrent.futures import Future

# Submitting a task to the async thread
# future = asyncThread.delayedRun(sample_function, 5, "Hello")

# Example of an async function
# async def asyncTask(self):
#    # Your async code here
#    print("Async task executed")
#    await asyncio.sleep(1)

# Submitting tasks to the async loop from the main thread
# future = run_async(asyncTask())

# Optional: wait for the task to complete, or you can continue with your main program
# result = future.result()

# # manual test:
# import thread
# at = thread.AsyncThread()
#
# at.repeatRun(task=print, interval=3, x='hello')
# at.repeatRun(task=print, interval=2, x='world')
