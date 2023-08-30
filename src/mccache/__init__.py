# See MIT license at the bottom of this script.
#
"""
This is a distributed application cache build on top of the `cachetools` package.  SEE: https://pypi.org/project/cachetools/
It uses UDP multicasting hence the name "Multi-Cast Cache", playfully abbreviated to "McCache".

When an object is insert/update an object into the cache, it is assumed that the instance shall be the latest.
McCache default behaviour is to notify all member in the cluster of this update and the other members shall evict their entry in their local cache.
If an entry is no longer in cache, re-calculation/processing or retrieve from storage is required.
The state of your object in storage should be the latest.

The worst case sceenario is an extra re-processing or retrieval from storage.
The best  case sceenario is that NO external call shall be made.
Once configured in more optimism mode then the default, more sophisticate cache coherence protocol shall be used.

This tool can be a good fit if your use cases meet most of the following guidelines:
    1. Not to be "dependent" on an external caching service such as memcached ,redis or riak.
    2. Keep the programming API "consistent" working with local cache.
    3. A small local network cluster of nodes running "identical" piece of software and setup.
    4. Number of objects to cache are not "many".
    5. Changes to the cache objects are not "frequent".
    6. Size of the value to be cache is "smaller" than the ethernet MTU (< 1472 bytes).
    7. Your load balancer could be configured to support "sticky" session.

Having stated the above, you need to quantify the above guidelines to match your use case.
There are 3 levels of optimism on the cache notification.  They are:

### 1. PESSIMISTIC:
    * All communication will required acknowledgement.
    * Changes to local cache shall propagate an eviction to the other member's local cache.
        - Without an entry in the member's local cache ,they have to re-fetch it from persistance storage, which should be the freshest.
    *`Total Time to Live` cache algorithmn.  Default is 15 minutes.
    * Multicast out 4 messages at 0, 10 ,30 ms apart

### 2. NEUTRAL (default):
    * No acknowledgement is required from the other members.
    * Changes to local cache will propagate an update to the other member's local cache.
        - Members with an existing local entry shall update their own local cache.
    *`Least Recently Used` cache algorithmn.
    * Multicast out 3 messages at 0 ,10 ms apart.

### 3. OPTIMISIC:
    * No acknowledgement is required from the other members.
    * Keep a global coherent cache.
        - All members shall update their own local cache with the multicasted change.
        - Increased size of cache for more objects.
    *`Least Recently Used` cache algorithmn.
    * Multicast out 2 message at 0 ms apart.
"""
__app__     = "McCache"
__author__  = "Edward Lau<elau1004@netscape.net>"
__version__ = "0.1.0"
__date__    = "2023-07-01"

import base64
import collections  # For cachetools.
import datetime
import functools    # For cachetools.
import hashlib
import heapq        # For cachetools.
import logging
import os
import pickle
import queue
import random
import socket
import struct
import threading
import time

import cachetools

from dataclasses import dataclass
from enum import Enum


# Cachetools section.

# McCache classes cloned from cachetools package version 5.3.1.
# Tried subclassing to overwrite:
#   __setitem__()
#   __delitem__()
#
# but encountered the issue where some of the value are set to None.  Did not encounter this issue using a straight cachetools package.
#
class _DefaultSize:
    __slots__ = ()

    def __getitem__(self, _):
        return 1

    def __setitem__(self, _, value):
        assert value == 1

    def pop(self, _):
        return 1


class Cache(collections.abc.MutableMapping):
    """Mutable mapping to serve as a simple cache or cache base class."""

    __marker = object()

    __size = _DefaultSize()

    __name:str = None

    def __init__(self, maxsize, getsizeof=None):
        if getsizeof:
            self.getsizeof = getsizeof
        if self.getsizeof is not Cache.getsizeof:
            self.__size = dict()
        self.__data = dict()
        self.__currsize = 0
        self.__maxsize = maxsize

    def __repr__(self):
        return f"{self.__class__.__name__}({repr(self.__data)} ,maxsize={self.__maxsize} ,currsize={self.__currsize}))"

    def __getitem__(self, key):
        try:
            return self.__data[key]
        except KeyError:
            return self.__missing__(key)

    def __setitem__(self, key, value, multicast:bool = True):
        maxsize = self.__maxsize
        size = self.getsizeof(value)
        if size > maxsize:
            raise ValueError("value too large")
        if key not in self.__data or self.__size[key] < size:
            while self.__currsize + size > maxsize:
                self.popitem()
        if key in self.__data:
            diffsize = size - self.__size[key]
        else:
            diffsize = size
        self.__data[key] = value
        self.__size[key] = size
        self.__currsize += diffsize

        # McCache addition.
        if  multicast:
            if  _config.op_level == McCacheLevel.OPTIMISTIC.value:  # Distribute the cache entry to remote members.
                _mcQueue.put((OpCode.PUT.name ,time.time_ns() ,self.name ,key ,value))
            elif _config.op_level == McCacheLevel.NEUTRAL.value:    # Update remote member's cache entry if exist.
                _mcQueue.put((OpCode.UPD.name ,time.time_ns() ,self.name ,key ,value))
            else:   # Evict the remote member's cache entry.
                _mcQueue.put((OpCode.DEL.name ,time.time_ns() ,self.name ,key ,None))

    def __delitem__(self, key, multicast:bool = True):
        size = self.__size.pop(key)
        del self.__data[key]
        self.__currsize -= size

        # McCache addition.
        if  multicast:
            _mcQueue.put((OpCode.DEL.name ,time.time_ns() ,self.name ,key ,None))

    def __contains__(self, key):
        return key in self.__data

    def __missing__(self, key):
        raise KeyError(key)

    def __iter__(self):
        return iter(self.__data)

    def __len__(self):
        return len(self.__data)

    def get(self, key, default=None):
        if key in self:
            return self[key]
        else:
            return default

    def pop(self, key, default=__marker):
        if key in self:
            value = self[key]
            del self[key]
        elif default is self.__marker:
            raise KeyError(key)
        else:
            value = default
        return value

    def setdefault(self, key, default=None):
        if key in self:
            value = self[key]
        else:
            self[key] = value = default
        return value

    def setname(self, name):
        """Set name of the cache."""
        self.__name = name

    @property
    def name(self):
        """The name of the cache."""
        return self.__name

    @property
    def maxsize(self):
        """The maximum size of the cache."""
        return self.__maxsize

    @property
    def currsize(self):
        """The current size of the cache."""
        return self.__currsize

    @staticmethod
    def getsizeof(value):
        """Return the size of a cache element's value."""
        return 1

class FIFOCache(Cache):
    """First In First Out (FIFO) cache implementation."""

    def __init__(self, maxsize, getsizeof=None):
        Cache.__init__(self, maxsize, getsizeof)
        self.__order = collections.OrderedDict()

    def __setitem__(self, key, value, cache_setitem=Cache.__setitem__):
        cache_setitem(self, key, value)
        try:
            self.__order.move_to_end(key)
        except KeyError:
            self.__order[key] = None

    def __delitem__(self, key, cache_delitem=Cache.__delitem__):
        cache_delitem(self, key)
        del self.__order[key]

    def popitem(self):
        """Remove and return the `(key, value)` pair first inserted."""
        try:
            key = next(iter(self.__order))
        except StopIteration:
            raise KeyError("%s is empty" % type(self).__name__) from None
        else:
            return (key, self.pop(key))


class LFUCache(Cache):
    """Least Frequently Used (LFU) cache implementation."""

    def __init__(self, maxsize, getsizeof=None):
        Cache.__init__(self, maxsize, getsizeof)
        self.__counter = collections.Counter()

    def __getitem__(self, key, cache_getitem=Cache.__getitem__):
        value = cache_getitem(self, key)
        if key in self:  # __missing__ may not store item
            self.__counter[key] -= 1
        return value

    def __setitem__(self, key, value, cache_setitem=Cache.__setitem__):
        cache_setitem(self, key, value)
        self.__counter[key] -= 1

    def __delitem__(self, key, cache_delitem=Cache.__delitem__):
        cache_delitem(self, key)
        del self.__counter[key]

    def popitem(self):
        """Remove and return the `(key, value)` pair least frequently used."""
        try:
            ((key, _),) = self.__counter.most_common(1)
        except ValueError:
            raise KeyError("%s is empty" % type(self).__name__) from None
        else:
            return (key, self.pop(key))


class LRUCache(Cache):
    """Least Recently Used (LRU) cache implementation."""

    def __init__(self, maxsize, getsizeof=None):
        Cache.__init__(self, maxsize, getsizeof)
        self.__order = collections.OrderedDict()

    def __getitem__(self, key, cache_getitem=Cache.__getitem__):
        value = cache_getitem(self, key)
        if key in self:  # __missing__ may not store item
            self.__update(key)
        return value

    def __setitem__(self, key, value, cache_setitem=Cache.__setitem__):
        cache_setitem(self, key, value)
        self.__update(key)

    def __delitem__(self, key, cache_delitem=Cache.__delitem__):
        cache_delitem(self, key)
        del self.__order[key]

    def popitem(self):
        """Remove and return the `(key, value)` pair least recently used."""
        try:
            key = next(iter(self.__order))
        except StopIteration:
            raise KeyError("%s is empty" % type(self).__name__) from None
        else:
            return (key, self.pop(key))

    def __update(self, key):
        try:
            self.__order.move_to_end(key)
        except KeyError:
            self.__order[key] = None


class MRUCache(Cache):
    """Most Recently Used (MRU) cache implementation."""

    def __init__(self, maxsize, getsizeof=None):
        Cache.__init__(self, maxsize, getsizeof)
        self.__order = collections.OrderedDict()

    def __getitem__(self, key, cache_getitem=Cache.__getitem__):
        value = cache_getitem(self, key)
        if key in self:  # __missing__ may not store item
            self.__update(key)
        return value

    def __setitem__(self, key, value, cache_setitem=Cache.__setitem__):
        cache_setitem(self, key, value)
        self.__update(key)

    def __delitem__(self, key, cache_delitem=Cache.__delitem__):
        cache_delitem(self, key)
        del self.__order[key]

    def popitem(self):
        """Remove and return the `(key, value)` pair most recently used."""
        try:
            key = next(iter(self.__order))
        except StopIteration:
            raise KeyError("%s is empty" % type(self).__name__) from None
        else:
            return (key, self.pop(key))

    def __update(self, key):
        try:
            self.__order.move_to_end(key, last=False)
        except KeyError:
            self.__order[key] = None


class RRCache(Cache):
    """Random Replacement (RR) cache implementation."""

    def __init__(self, maxsize, choice=random.choice, getsizeof=None):
        Cache.__init__(self, maxsize, getsizeof)
        self.__choice = choice

    @property
    def choice(self):
        """The `choice` function used by the cache."""
        return self.__choice

    def popitem(self):
        """Remove and return a random `(key, value)` pair."""
        try:
            key = self.__choice(list(self))
        except IndexError:
            raise KeyError("%s is empty" % type(self).__name__) from None
        else:
            return (key, self.pop(key))


class _TimedCache(Cache):
    """Base class for time aware cache implementations."""

    class _Timer:
        def __init__(self, timer):
            self.__timer = timer
            self.__nesting = 0

        def __call__(self):
            if self.__nesting == 0:
                return self.__timer()
            else:
                return self.__time

        def __enter__(self):
            if self.__nesting == 0:
                self.__time = time = self.__timer()
            else:
                time = self.__time
            self.__nesting += 1
            return time

        def __exit__(self, *exc):
            self.__nesting -= 1

        def __reduce__(self):
            return _TimedCache._Timer, (self.__timer,)

        def __getattr__(self, name):
            return getattr(self.__timer, name)

    def __init__(self, maxsize, timer=time.monotonic, getsizeof=None):
        Cache.__init__(self, maxsize, getsizeof)
        self.__timer = _TimedCache._Timer(timer)

    def __repr__(self, cache_repr=Cache.__repr__):
        with self.__timer as time:
            self.expire(time)
            return cache_repr(self)

    def __len__(self, cache_len=Cache.__len__):
        with self.__timer as time:
            self.expire(time)
            return cache_len(self)

    @property
    def currsize(self):
        with self.__timer as time:
            self.expire(time)
            return super().currsize

    @property
    def timer(self):
        """The timer function used by the cache."""
        return self.__timer

    def clear(self):
        with self.__timer as time:
            self.expire(time)
            Cache.clear(self)

    def get(self, *args, **kwargs):
        with self.__timer:
            return Cache.get(self, *args, **kwargs)

    def pop(self, *args, **kwargs):
        with self.__timer:
            return Cache.pop(self, *args, **kwargs)

    def setdefault(self, *args, **kwargs):
        with self.__timer:
            return Cache.setdefault(self, *args, **kwargs)


class TTLCache(_TimedCache):
    """LRU Cache implementation with per-item time-to-live (TTL) value."""

    class _Link:

        __slots__ = ("key", "expires", "next", "prev")

        def __init__(self, key=None, expires=None):
            self.key = key
            self.expires = expires

        def __reduce__(self):
            return TTLCache._Link, (self.key, self.expires)

        def unlink(self):
            next = self.next
            prev = self.prev
            prev.next = next
            next.prev = prev

    def __init__(self, maxsize, ttl, timer=time.monotonic, getsizeof=None):
        _TimedCache.__init__(self, maxsize, timer, getsizeof)
        self.__root = root = TTLCache._Link()
        root.prev = root.next = root
        self.__links = collections.OrderedDict()
        self.__ttl = ttl

    def __contains__(self, key):
        try:
            link = self.__links[key]  # no reordering
        except KeyError:
            return False
        else:
            return self.timer() < link.expires

    def __getitem__(self, key, cache_getitem=Cache.__getitem__):
        try:
            link = self.__getlink(key)
        except KeyError:
            expired = False
        else:
            expired = not (self.timer() < link.expires)
        if expired:
            return self.__missing__(key)
        else:
            return cache_getitem(self, key)

    def __setitem__(self, key, value, cache_setitem=Cache.__setitem__):
        with self.timer as time:
            self.expire(time)
            cache_setitem(self, key, value)
        try:
            link = self.__getlink(key)
        except KeyError:
            self.__links[key] = link = TTLCache._Link(key)
        else:
            link.unlink()
        link.expires = time + self.__ttl
        link.next = root = self.__root
        link.prev = prev = root.prev
        prev.next = root.prev = link

    def __delitem__(self, key, cache_delitem=Cache.__delitem__):
        cache_delitem(self, key)
        link = self.__links.pop(key)
        link.unlink()
        if not (self.timer() < link.expires):
            raise KeyError(key)

    def __iter__(self):
        root = self.__root
        curr = root.next
        while curr is not root:
            # "freeze" time for iterator access
            with self.timer as time:
                if time < curr.expires:
                    yield curr.key
            curr = curr.next

    def __setstate__(self, state):
        self.__dict__.update(state)
        root = self.__root
        root.prev = root.next = root
        for link in sorted(self.__links.values(), key=lambda obj: obj.expires):
            link.next = root
            link.prev = prev = root.prev
            prev.next = root.prev = link
        self.expire(self.timer())

    @property
    def ttl(self):
        """The time-to-live value of the cache's items."""
        return self.__ttl

    def expire(self, time=None):
        """Remove expired items from the cache."""
        if time is None:
            time = self.timer()
        root = self.__root
        curr = root.next
        links = self.__links
        cache_delitem = Cache.__delitem__
        while curr is not root and not (time < curr.expires):
            cache_delitem(self, curr.key)
            del links[curr.key]
            next = curr.next
            curr.unlink()
            curr = next

    def popitem(self):
        """Remove and return the `(key, value)` pair least recently used that
        has not already expired.

        """
        with self.timer as time:
            self.expire(time)
            try:
                key = next(iter(self.__links))
            except StopIteration:
                raise KeyError("%s is empty" % type(self).__name__) from None
            else:
                return (key, self.pop(key))

    def __getlink(self, key):
        value = self.__links[key]
        self.__links.move_to_end(key)
        return value


class TLRUCache(_TimedCache):
    """Time aware Least Recently Used (TLRU) cache implementation."""

    @functools.total_ordering
    class _Item:

        __slots__ = ("key", "expires", "removed")

        def __init__(self, key=None, expires=None):
            self.key = key
            self.expires = expires
            self.removed = False

        def __lt__(self, other):
            return self.expires < other.expires

    def __init__(self, maxsize, ttu, timer=time.monotonic, getsizeof=None):
        _TimedCache.__init__(self, maxsize, timer, getsizeof)
        self.__items = collections.OrderedDict()
        self.__order = []
        self.__ttu = ttu

    def __contains__(self, key):
        try:
            item = self.__items[key]  # no reordering
        except KeyError:
            return False
        else:
            return self.timer() < item.expires

    def __getitem__(self, key, cache_getitem=Cache.__getitem__):
        try:
            item = self.__getitem(key)
        except KeyError:
            expired = False
        else:
            expired = not (self.timer() < item.expires)
        if expired:
            return self.__missing__(key)
        else:
            return cache_getitem(self, key)

    def __setitem__(self, key, value, cache_setitem=Cache.__setitem__):
        with self.timer as time:
            expires = self.__ttu(key, value, time)
            if not (time < expires):
                return  # skip expired items
            self.expire(time)
            cache_setitem(self, key, value)
        # removing an existing item would break the heap structure, so
        # only mark it as removed for now
        try:
            self.__getitem(key).removed = True
        except KeyError:
            pass
        self.__items[key] = item = TLRUCache._Item(key, expires)
        heapq.heappush(self.__order, item)

    def __delitem__(self, key, cache_delitem=Cache.__delitem__):
        with self.timer as time:
            # no self.expire() for performance reasons, e.g. self.clear() [#67]
            cache_delitem(self, key)
        item = self.__items.pop(key)
        item.removed = True
        if not (time < item.expires):
            raise KeyError(key)

    def __iter__(self):
        for curr in self.__order:
            # "freeze" time for iterator access
            with self.timer as time:
                if time < curr.expires and not curr.removed:
                    yield curr.key

    @property
    def ttu(self):
        """The local time-to-use function used by the cache."""
        return self.__ttu

    def expire(self, time=None):
        """Remove expired items from the cache."""
        if time is None:
            time = self.timer()
        items = self.__items
        order = self.__order
        # clean up the heap if too many items are marked as removed
        if len(order) > len(items) * 2:
            self.__order = order = [item for item in order if not item.removed]
            heapq.heapify(order)
        cache_delitem = Cache.__delitem__
        while order and (order[0].removed or not (time < order[0].expires)):
            item = heapq.heappop(order)
            if not item.removed:
                cache_delitem(self, item.key)
                del items[item.key]

    def popitem(self):
        """Remove and return the `(key, value)` pair least recently used that
        has not already expired.

        """
        with self.timer as time:
            self.expire(time)
            try:
                key = next(iter(self.__items))
            except StopIteration:
                raise KeyError("%s is empty" % self.__class__.__name__) from None
            else:
                return (key, self.pop(key))

    def __getitem(self, key):
        value = self.__items[key]
        self.__items.move_to_end(key)
        return value


# McCache Section.

class McCacheLevel(Enum):
    PESSIMISTIC = 3 # Something out there is going to screw you.  Requires acknowledgement.  Evict the caches.
    NEUTRAL     = 5 # Default.
    OPTIMISTIC  = 7 # Life is great on the happy path.  Acknowledgment is not required.  Sync the caches.


class OpCode(Enum):
    # Keep everythng here as 3 character fixed length strings.
    ACK = 'ACK'     # Acknowledgement of a received request.
    BYE = 'BYE'     # Member announcing it is leaving the group.
    DEL = 'DEL'     # Member requesting the group to evict the cache entry.
    ERR = 'ERR'     # Member announcing an error to the group.
    INI = 'INI'     # Member announcing its initialization to the group.
    INQ = 'INQ'     # Member inquiring about a cache entry from the group.
    NEW = 'NEW'     # New member annoucement to join the group.
    NOP = 'NOP'     # No operation.
    PUT = 'PUT'     # Member annoucing a new cache entry is put into its local cache.
    QRY = 'QRY'     # Query the cache.
    RST = 'RST'     # Reset the cache.
    UPD = 'UPD'     # Update an existing cache entry.


@dataclass
class McCache_Config():
    ttl: int  = 900             # Total Time to Live in seconds for a cached entry.
    mc_gip: str = '224.0.0.3'   # Unassigned multi-cast IP.
    mc_port: int = 4000         # Unofficial port.  Was Diablo II game.
    mc_hops: int = 1            # Only local subnet.
    max_size: int = 2048        # Entries.
    op_level: int = McCacheLevel.NEUTRAL.value
    debug_log: str = None       # Full pathname of the log file.
    house_keeping_slots: str = '5,8,13,21,55' # Periods for first 5 slots: Very frequent ,Frequent ,Normal ,Slow ,Very slow.


# Module initialization.
#
_lock = threading.RLock()   # Module-level lock for serializing access to shared data.
_mcCacheDict = {}   # Private dict to segregate the cache namespace.
_mcPending = {}     # Pending acknowledgement inpessivemistic mode. Use 'cachetools.TTLCache' instead.
_mcMember = {}      # Members in the group.  ID/Timestamp.
_mcQueue = queue.Queue()
_mcIPAdd = {
    224: {
        0: {
            # Local Network.
            0:  set({3 ,26 ,255}).union({d for d in range(69 ,101)}).union({d for d in range(122 ,150)}).union({d for d in range(151 ,251)}),
            # Adhoc Block I.
            2:  set({0}).union({d for d in range(18 ,64)}),
            6:  set({d for d in range(145 ,161)}).union({d for d in range(152 ,192)}),
            12: set({d for d in range(136 ,256)}),
            17: set({d for d in range(128 ,256)}),
            20: set({d for d in range(208 ,256)}),
            21: set({d for d in range(128 ,256)}),
            23: set({d for d in range(182 ,192)}),
            245:set({d for d in range(0   ,256)}),
            # TODO: Adhoc Block II.
            # TODO: Adhoc Block III.
        },
    }
}

ipv4: str = socket.getaddrinfo(socket.gethostname() ,0 ,socket.AF_INET )[0][4][0]
ipV4: str = "".join([hex(int(g)).removeprefix("0x").zfill(2) for g in ipv4.split(".")])    # Uppercase "V"
ipv6: str = None
ipV6: str = None
try:
    ipv6: str = socket.getaddrinfo(socket.gethostname() ,0 ,socket.AF_INET6)[0][4][0]
    ipV6: str = ipv6.replace(':' ,'')   # Uppercase "V"
except Exception:
    pass

SOURCE_ADD = f"{ipv4}:{os.getpid()}"
LOG_FORMAT = f"%(asctime)s.%(msecs)03d (%(ipV4)s.%(process)d.%(thread)05d)[%(levelname)s {__app__}] %(message)s"
LOG_EXTRA = {'ipv4': ipV4 ,'ipV4': ipV4 ,'ipv6': ipv6 ,'ipV6': ipV6}    # Extra fields for the logger mesage.
logger = logging.getLogger()    # Root logger.


# Configure McCache.
#
_config = McCache_Config()

try:
    LOG_FORMAT = os.environ['MCCACHE_LOG_FORMAT']
except:
    pass
try:
    _config.ttl = int(os.environ['MCCACHE_TTL'])
except:
    pass
try:
    _config.op_level = int(os.environ['MCCACHE_LEVEL'])
    if  _config.op_level == McCacheLevel.PESSIMISTIC:
        _config.max_size =  1024
    if  _config.op_level == McCacheLevel.OPTIMISTIC:
        _config.max_size =  4096
except:
    pass
try:
    _config.max_size = int(os.environ['MCCACHE_MAXSIZE'])
except:
    pass
try:
    _config.house_keeping_slots = int(os.environ['MCCACHE_SLOTS'])
except:
    pass
try:
    _config.debug_log = os.environ['MCCACHE_DEBUG_FILE']
except:
    pass
try:
    _config.mc_hops = int(os.environ['MCCACHE_MULTICAST_HOPS'])
except:
    pass
_ip = None
try:
    # SEE: https://www.iana.org/assignments/multicast-addresses/multicast-addresses.xhtml
    if 'MCCACHE_MULTICAST_IP' in os.environ['MCCACHE_MULTICAST_IP']:
        if ':' not in os.environ['MCCACHE_MULTICAST_IP']:
            _config.mc_gip  = os.environ['MCCACHE_MULTICAST_IP']
        else:
            _config.mc_gip  = os.environ['MCCACHE_MULTICAST_IP'].split(':')[0]
            _config.mc_port = int(os.environ['MCCACHE_MULTICAST_IP'].split(':')[1])

        _ip = [int(d) for d in _config.mc_gip.split(".")]
        if  len(_ip) != 4 or _ip[0] != 224:
            raise ValueError("{_mcCache_gip} is an invalid multicast IP address!")

        if  not(_ip[0] in _mcIPAdd and \
                _ip[1] in _mcIPAdd[_ip[0]] and \
                _ip[2] in _mcIPAdd[_ip[0]][_ip[1]] and \
                _ip[3] in _mcIPAdd[_ip[0]][_ip[1]][_ip[2]]
            ):
            raise ValueError("{_mcCache_gip} is an unavailable multicast IP address! See: https://tinyurl.com/4cymemdf")
except KeyError:
    pass
except ValueError as ex:
    logger.warning(f"{ex} Defaulting to IP: {_config.mc_gip}", extra=LOG_EXTRA)
finally:
    del _ip
    del _mcIPAdd

# Setup McCache logger.
#
logger = logging.getLogger('mccache')   # McCache specific logger.
logger.propagate = False
logger.setLevel( logging.INFO )
_hdlr = logging.StreamHandler()
_fmtr = logging.Formatter(fmt=LOG_FORMAT ,datefmt='%Y%m%d%a %H%M%S' ,defaults=LOG_EXTRA)
_hdlr.setFormatter(_fmtr)
logger.addHandler(_hdlr)
if  _config.debug_log:
    _hdlr = logging.FileHandler(_config.debug_log ,mode="a")
    _hdlr.setFormatter(_fmtr)
    logger.addHandler(_hdlr)
    logger.setLevel(logging.DEBUG)
del _hdlr
del _fmtr
logger.info(f"Setting: (level: {_config.op_level} ,size: {_config.max_size}) ,ttl: {_config.ttl} ,gip: {_config.mc_gip} ,dbg: {_config.debug_log is not None})")


# Public methods.
#
def getCache(name: str = None ,cache: Cache = None) -> Cache:
    """
    Return a cache with the specified name ,creating it if necessary.
    If no name is specified ,return the default TLRUCache or LRUCache cache depending on the optimism setting.
    SEE: https://dropbox.tech/infrastructure/caching-in-theory-and-practice

    Parameter
    ---------
        name: str       Name to isolate different caches.  Namespace dot notation is suggested.
        cache: Cache    Optional cache instance to override the default cache type.

    Return: Cache instance to use with given name.
    """
    if  name:
        if  not isinstance( name ,str ):
            raise TypeError('A cache name must be a string!')
    else:
        name = 'default'
    if  cache:
        if  not isinstance( cache ,Cache ):
            raise TypeError(f"Cache name '{name}' is not of type McCache.Cache!")
    try:
        _lock.acquire()
        if  name in _mcCacheDict:
            cache = _mcCacheDict[ name ]
        else:
            if  not cache:
                # This will be the default type of cache for McCache.
                if _config.op_level == McCacheLevel.PESSIMISTIC:
                    cache = TLRUCache( maxsize=_config.max_size ,ttl=_config.ttl )
                else:
                    cache = LRUCache( maxsize=_config.max_size )

            if  cache.name is None:
                cache.name = name
                _mcQueue.put((OpCode.INI.name ,time.time_ns() ,cache.name ,None ,None))

            _mcCacheDict[ name ] = cache
    finally:
        _lock.release()

    return cache


# Private thread methods.
#
def _multicaster() -> None:
    """
    Dequeue and multicast out the cache operation to all the members in the group.

    A message is constructed using the following format:
        OP Code:    Cache operation code.  SEE: OpCode enum.
        Timestamp:  When this request was generated in Python's nano seconds.
        Namespace:  Namespace of the cache.
        Key:        The key in the cache.
        CRC:        Checksum of the value identified by the key.
        Value:      The cached value.
    """    
    addrinfo = socket.getaddrinfo(_config.mc_gip, None)[0]

    sock = socket.socket(addrinfo[0], socket.SOCK_DGRAM)

    # Set Time-to-live (optional)
    ttl_bin = struct.pack('@i', _config.mc_hops)
    if  addrinfo[0] == socket.AF_INET: # IPv4
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, ttl_bin)
    else:
        sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_MULTICAST_HOPS, ttl_bin)

    # Keep the format consistent to make it easy  for the test to parse.
    msg = (OpCode.NEW.value ,None ,None ,None ,None ,'McCache broadcaster is ready.')
    logger.debug(f"Im:{SOURCE_ADD}\tFr:\tMsg:{msg}" ,extra=LOG_EXTRA)

    while True:
        try:
            msg = _mcQueue.get()
            opc = msg[0]    # Op Code
            tsm = msg[1]    # Timestamp
            nms = msg[2]    # Namespace
            key = msg[3]    # Key
            val = msg[4]    # Value
            crc = None
            if _config.op_level >= McCacheLevel.NEUTRAL.value and val is not None:
                pkl = pickle.dumps( val )
                crc = base64.a85encode( hashlib.md5( pkl ).digest() ,foldspaces=True).decode()
            if _config.op_level == McCacheLevel.PESSIMISTIC:
                val = None
            msg = (opc ,tsm ,nms ,key ,crc ,val)
            pkl = pickle.dumps( msg )   # Serialized out the tuple.

            if  logger.level == logging.DEBUG:
                logger.debug(f"Im:{SOURCE_ADD}\tFr:\tMsg:{msg}" ,extra=LOG_EXTRA)
            
            if  _config.op_level <= McCacheLevel.PESSIMISTIC.value:
                # Need to be acknowledged.
                if (nms ,key ,tsm) not in _mcPending:
                    _mcPending[(nms ,key ,tsm)] = val

            # UDP is not reliable.  Send 2 messages.
            sock.sendto( pkl ,(_config.mc_gip ,_config.mc_port))
            sock.sendto( pkl ,(_config.mc_gip ,_config.mc_port))

            if  _config.op_level <= McCacheLevel.NEUTRAL.value:
                time.sleep(0.01)    # 10 msec.
                sock.sendto( pkl ,(_config.mc_gip ,_config.mc_port))
            if  _config.op_level <= McCacheLevel.PESSIMISTIC.value:
                time.sleep(0.03)    # 30 msec.
                sock.sendto( pkl ,(_config.mc_gip ,_config.mc_port))

        except  Exception as ex:
            logger.error(ex)

def _housekeeper() -> None:
    """
    Background house keeping thread.
    """
    _mcQueue.put((OpCode.NEW.name ,time.time_ns() ,None ,None ,None))

    # Keep the format consistent to make it easy  for the test to parse.
    msg = (OpCode.NEW.value ,None ,None ,None ,None ,'McCache housekeeper is ready.')
    logger.debug(f"Im:{SOURCE_ADD}\tFr:\tMsg:{msg}" ,extra=LOG_EXTRA)

    # TODO: Acknowledge the sender if required.

def _listener() -> None:
    """
    Listen in the group for new cache operation from all members.
    """
    # socket.AF_INET:           IPv4
    # socket.SOL_SOCKET:        The socket layer itself.
    # socket.IPPROTO_IP:        Value is 0 which is the default and creates a socket that will receive only IP packet.
    # socket.INADDR_ANY:        Binds the socket to all available local interfaces.
    # socket.SO_REUSEADDR:      Tells the kernel to reuse a local socket in TIME_WAIT state ,without waiting for its natural timeout to expire.
    # socket.IP_ADD_MEMBERSHIP: This tells the system to receive packets on the network whose destination is the group address (but not its own)
 
    addrinfo = socket.getaddrinfo(_config.mc_gip, None)[0]

    sock = socket.socket(addrinfo[0], socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(('', _config.mc_port))    # It need empty string.  If not it will throw an "The requested address is not valid in its context" exception.

    group_bin = socket.inet_pton(addrinfo[0], addrinfo[4][0])
    # Join multicast group
    if  addrinfo[0] == socket.AF_INET: # IPv4
        mreq = group_bin + struct.pack('=I', socket.INADDR_ANY)
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
    else:
        mreq = group_bin + struct.pack('@I', 0)
        sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_JOIN_GROUP, mreq)

    # Keep the format consistent to make it easy  for the test to parse.
    msg = (OpCode.NEW.value ,None ,None ,None ,None ,'McCache listener is ready.')
    logger.debug(f"Im:{SOURCE_ADD}\tFr:\tMsg:{msg}" ,extra=LOG_EXTRA)

    while True:
        try:
            pkl, sender = sock.recvfrom( 4096 )
            if  logger.level == logging.DEBUG:
                logger.debug(f"Im:{SOURCE_ADD}\tFr:{sender[0]}\tMsg:{msg}" ,extra=LOG_EXTRA)

            msg = pickle.loads( pkl )   # De-Serialized in the tuple.
            opc = msg[0]    # Op Code
            tsm = msg[1]    # Timestamp
            nms = msg[2]    # Namespace
            key = msg[3]    # Key
            crc = msg[4]    # CRC
            val = msg[5]    # Value

            if  sender[0] != ipv4:  # Ignore my own messages.
                mcc = getCache( nms )
                match opc:
                    case OpCode.ACK.value:
                        if (nms ,key ,tsm) in _mcPending:
                            del _mcPending[(nms ,key ,tsm)]
                    case OpCode.DEL.value:
                        if  key in mcc:
                            mcc.__delitem__( key ,False )
                    case OpCode.PUT.value | OpCode.UPD.value:
                        mcc.__setitem__( key ,val ,False )
                    case OpCode.INQ.value:
                        if  logger.level == logging.DEBUG:
                            # NOTE: Don't dump the raw data out for security reason.
                            if  key is  None:
                                keys = list(mcc.keys())
                                keys.sort()
                                _mc = {k: base64.a85encode( hashlib.md5( pickle.dumps( mcc[k] )).digest() ,foldspaces=True).decode() for k in keys}
                                msg = (opc ,None ,nms ,None ,None ,_mc)
                            else:
                                val = mcc.get( key ,None )
                                crc = base64.a85encode( hashlib.md5( pickle.dumps( val )).digest() ,foldspaces=True).decode()
                                msg = (opc ,None ,nms ,key ,crc ,None)
                            logger.debug(f"Im:{SOURCE_ADD}\tFr:{SOURCE_ADD}\tMsg:{msg}" ,extra=LOG_EXTRA)
                    case _:
                        pass
        except  Exception as ex:
            logger.error(ex)

# Main section to start the background daemon threads.
#
t1 = threading.Thread(target=_multicaster ,daemon=True)
t1.start()
t2 = threading.Thread(target=_housekeeper ,daemon=True)
t2.start()
t3 = threading.Thread(target=_listener    ,daemon=True)
t3.start()

if __name__ == "__main__":
    duration = 1
    entries = 20
    logger.setLevel(logging.DEBUG)
    cache = getCache()
    bgn = time.time()
    time.sleep( 1 )
    end = time.time()
    #while (end - bgn) < (duration*60):   # Seconds.
    for _ in range(0,60):
        time.sleep( random.randint(1 ,10)/10.0 )
        key = int((time.time_ns() /100) %entries)
        opc = random.randint(0 ,10)
        match opc:
            case 0:
                if  key in cache:
                    # Evict cache.
                    del cache[key]
            case 1|2|3:
                if  key not in cache:
                    # Insert cache.
                    cache[key] = datetime.datetime.utcnow()
            case 4|5|6|7:
                if  key in cache:
                    # Update cache.
                    cache[key] = datetime.datetime.utcnow()
            case _:
                # Look up cache.
                _ = cache.get( key ,None )
        end = time.time()
    time.sleep(3)

    keys = list(cache.keys())
    keys.sort()
    _mc = {k: cache[k] for k in keys}
    msg = (OpCode.QRY.name ,None ,cache.name ,None ,None ,_mc)
    logger.debug(f"Im:{SOURCE_ADD}\tFr:{SOURCE_ADD}\tMsg:{msg}" ,extra=LOG_EXTRA)


# The MIT License (MIT)
# Copyright (c) 2023 McCache authors.
# 
# Permission is hereby granted ,free of charge ,to any person obtaining a copy
# of this software and associated documentation files (the "Software") ,to deal
# in the Software without restriction ,including without limitation the rights
# to use ,copy ,modify ,merge ,publish ,distribute ,sublicense ,and/or sell
# copies of the Software ,and to permit persons to whom the Software is
# furnished to do so ,subject to the following conditions:
# 
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
# 
# THE SOFTWARE IS PROVIDED "AS IS" ,WITHOUT WARRANTY OF ANY KIND,
# EXPRESS OR IMPLIED ,INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF
# MERCHANTABILITY ,FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT.
# IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM,
# DAMAGES OR OTHER LIABILITY ,WHETHER IN AN ACTION OF CONTRACT ,TORT OR
# OTHERWISE ,ARISING FROM ,OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE
# OR OTHER DEALINGS IN THE SOFTWARE.
