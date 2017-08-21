'''eventlog -- structured logging support

EventLogger for nested events
=============================

Suppose we want to log some events which naturally nest.

First, add a handler to a logger:

>>> import logging, sys
>>> log1 = logging.getLogger('log1')
>>> detail = logging.StreamHandler(sys.stdout)
>>> log1.addHandler(detail)
>>> log1.setLevel(logging.INFO)
>>> detail.setFormatter(logging.Formatter(
...     fmt='%(levelname)s %(elapsed)s %(do)s %(message)s'))

>>> io = MockIO()
>>> event0 = EventLogger(log1, dict(customer='Jones', invoice=123), io.clock)


>>> with event0.step('Build %(product)s', dict(product='house')):
...     with event0.step('lay foundation %(depth)d ft deep',
...                      dict(depth=20)) as info:
...         info.msg_parts.append(' at %(temp)d degrees')
...         info.argobj['temp'] = 65
...     with event0.step('frame %(stories)d story house',
...                      dict(stories=2)) as info:
...         pass
...     start, elapsed, _ms = event0.elapsed()
...     eta = event0.eta(pct=25)
... # doctest: +ELLIPSIS
INFO ('... 12:30:01', None, None) begin 0:00:00 [1] Build house...
INFO ('... 12:30:03', None, None) begin 0:00:02 [1, 2] lay foundation 20 ft deep...
INFO ('... 12:30:03', '0:00:03', 3000000) end 0:00:03 [1, 2] lay foundation 20 ft deep at 65 degrees.
INFO ('... 12:30:10', None, None) begin 0:00:09 [1, 3] frame 2 story house...
INFO ('... 12:30:10', '0:00:05', 5000000) end 0:00:05 [1, 3] frame 2 story house.
INFO ('... 12:30:01', '0:00:35', 35000000) end 0:00:35 [1] Build house.

>>> eta
datetime.datetime(2000, 1, 1, 12, 31, 49)

'''

from contextlib import contextmanager
from datetime import datetime
from typing import (
    Any, Callable, Dict, Iterator, List, MutableMapping,
    NamedTuple, Optional as Opt, TextIO, Tuple
)
import logging
from queue import Queue, Empty
from threading import Timer


KWArgs = MutableMapping[str, Any]
JSONObject = Dict[str, Any]

LogState = NamedTuple('LogState', [
    ('msg_parts', List[str]),
    ('argobj', JSONObject),
    ('extra', JSONObject)])


class EventLogger(logging.LoggerAdapter):
    def __init__(self, logger: logging.Logger, event: JSONObject,
                 clock: Opt[Callable[[], datetime]]=None) -> None:
        logging.LoggerAdapter.__init__(self, logger, extra={})
        self.name = logger.name
        if clock is None:
            clock = datetime.now  # ISSUE: ambient
        self.event = event
        self._clock = clock
        self._seq = 0
        self._step = []  # type: List[Tuple[int, datetime]]
        List  # let flake8 know we're using it

    def __repr__(self) -> str:
        return '%s(%s, %s)' % (self.__class__.__name__, self.name, self.event)

    def process(self, msg: str, kwargs: KWArgs) -> Tuple[str, KWArgs]:
        extra = dict(kwargs.get('extra', {}),
                     context=self.event)
        return msg, dict(kwargs, extra=extra)

    def elapsed(self, then: Opt[datetime]=None) -> Tuple[str, str, int]:
        start = then or self._step[-1][1]
        elapsed = self._clock() - start
        ms = int(elapsed.total_seconds() * 1000000)
        return (str(start), str(elapsed), ms)

    def eta(self, pct: float) -> datetime:
        t0 = self._step[0][1]
        elapsed = self._clock() - t0
        return t0 + elapsed * (1 / (pct / 100))

    @contextmanager
    def step(self, msg: str, argobj: Dict[str, object],
             extra: Opt[Dict[str, object]]=None) -> Iterator[LogState]:
        checkpoint = self._clock()
        self._seq += 1
        self._step.append((self._seq, checkpoint))
        extra = extra or {}
        fmt_step = '%(t_step)s %(step)s '
        step_ixs = [ix for (ix, _t) in self._step]
        t_step = str(checkpoint - self._step[0][1])
        self.info(fmt_step + msg + '...',
                  dict(argobj, step=step_ixs, t_step=t_step),
                  extra=dict(extra, do='begin',
                             elapsed=(str(checkpoint), None, None)))
        msgparts = [msg]
        outcome = logging.INFO
        try:
            yield LogState(msgparts, argobj, extra)
        except:
            outcome = logging.ERROR
            raise
        finally:
            elapsed = self.elapsed(then=checkpoint)
            self.log(outcome, ''.join([fmt_step] + msgparts) + '.',
                     dict(argobj, step=step_ixs, t_step=elapsed[1]),
                     extra=dict(extra, do='end',
                                elapsed=elapsed))
            self._step.pop()


class TextFilter(logging.Filter):
    def __init__(self, skips: List[str]) -> None:
        self.skips = skips

    def filter(self, record: logging.LogRecord) -> bool:
        for skip in self.skips:
            if record.getMessage().startswith(skip):
                return False
        return True


class TextHandler(logging.StreamHandler):
    def __init__(self, stream: TextIO, skips: List[str]=[]) -> None:
        logging.StreamHandler.__init__(self, stream)
        self.addFilter(TextFilter(skips))


class DebounceHandler(logging.Handler):
    '''Only log after logging has gone quiet after an interval.
    '''
    # ISSUE: not unit testable

    # Note luigi runs multiple workers each in their own process,
    # and we'll have a separate timer per process.

    def __init__(self, capacity: int=2, interval: float=0.5,
                 flushLevel: int=logging.WARN,
                 target: logging.Handler=None) -> None:
        logging.Handler.__init__(self)
        self.target = target or logging.StreamHandler()  # ISSUE: ambient
        self.buffer = Queue(maxsize=capacity)  # type: Queue[logging.LogRecord]
        self.interval = interval
        self.flushLevel = flushLevel
        self._timer = None  # type: Opt[Timer]
        self.start()  # ISSUE: I/O in a constructor

    def setFormatter(self, form: logging.Formatter) -> None:
        self.target.setFormatter(form)

    def start(self) -> None:
        timer = Timer(self.interval, self.flush)
        self._timer = timer
        timer.start()

    def stop(self) -> None:
        timer = self._timer
        if timer:
            timer.cancel()
            self._timer = None
        self.flush()

    def emit(self, record: logging.LogRecord) -> None:
        timer = self._timer
        if timer:
            timer.cancel()
        if self.buffer.full():
            # timeout could sneak in here, so don't block
            self.buffer.get(block=False)
        # We're the only producer, so this can't block.
        self.buffer.put(record)

        if record.levelno >= self.flushLevel:
            self.flush()
        else:
            self.start()

    def flush(self) -> None:
        while not self.buffer.empty():
            try:
                record = self.buffer.get()
                self.target.handle(record)
            except Empty:
                pass


class MockIO(object):
    def __init__(self,
                 now: datetime=datetime(2000, 1, 1, 12, 30, 0)) -> None:
        self._now = now
        self._delta = 1

    def clock(self) -> datetime:
        from datetime import timedelta
        self._now += timedelta(seconds=self._delta)
        self._delta += 1
        return self._now


def _integration_test(stderr: TextIO, sleep: Callable[[int], None]) -> None:
    log1 = logging.getLogger('log1')
    d = DebounceHandler(capacity=2)
    d.start()
    log1.addHandler(d)
    log1.setLevel(logging.INFO)
    log1.info('quick one')
    log1.info('quick two')
    log1.info('slow one')
    sleep(2)
    log1.info('2quick one')
    log1.info('2quick two')
    log1.info('slow two')
    sleep(2)
    log1.info('last one')
    d.stop()


if __name__ == '__main__':
    def _script() -> None:
        from sys import stderr
        from time import sleep

        _integration_test(stderr, sleep)

    _script()
