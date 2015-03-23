#!/usr/bin/env python

# * mpd-findadd-duration.py

# ** Imports
import argparse
from collections import defaultdict
import logging
import random
import re
import sys
from threading import Thread
import time

import mpd  # Using python-mpd2

# Verify python-mpd2 is being used
if mpd.VERSION < (0, 5, 4):
    print 'ERROR: This script requires python-mpd2 >= 0.5.4.'
    sys.exit(1)

# ** Constants
DEFAULT_PORT = 6600
FILE_PREFIX_RE = re.compile('^file: ')

# ** Classes
class Playlist(list):
    def __init__(self, *args, **kwargs):
        super(Playlist, self).__init__(args)
        self.duration = sum([int(track['time']) for track in args]) if args else 0

    def append(self, item):
        super(Playlist, self).append(item)

        # TODO: Is there a more Pythonic way to do this?
        self.duration += sum([int(track['time']) for track in item]) if isinstance(item, list) or isinstance(item, set) else int(item['time'])

    def extend(self, item):
        super(Playlist, self).extend(item)

        # TODO: Is there a more Pythonic way to do this?
        self.duration += sum([int(track['time']) for track in item]) if isinstance(item, list) or isinstance(item, set) else int(item['time'])
        
class MyFloat(float):
    '''Rounds and pads to 3 decimal places when printing.  Also overrides
    built-in operator methods to return myFloats instead of regular
    floats.'''

    # There must be a better, cleaner way to do this, maybe using
    # decorators or overriding __metaclass__, but I haven't been able
    # to figure it out.  Since you can't override float's methods, you
    # can't simply override __str__, for all floats.  And because
    # whenever you +|-|/|* on a subclassed float, it returns a regular
    # float, you have to also override those built-in methods to keep
    # returning the subclass.

    def __init__(self, num, roundBy=3):
        super(MyFloat, self).__init__(num)
        self.roundBy = roundBy

    def __abs__(self):
        return MyFloat(float.__abs__(self))

    def __add__(self, val):
        return MyFloat(float.__add__(self, val))

    def __div__(self, val):
        return MyFloat(float.__div__(self, val))

    def __mul__(self, val):
        return MyFloat(float.__mul__(self, val))

    def __sub__(self, val):
        return MyFloat(float.__sub__(self, val))

    def __str__(self):
        return "{:.3f}".format(round(self, self.roundBy))

    # __repr__ is used in, e.g. mpd.seek(), so it gets a rounded
    # float.  MPD doesn't support more than 3 decimal places, anyway.
    __repr__ = __str__

class AveragedList(list):

    def __init__(self, data=None, length=None, name=None, printDebug=False):
        self.log = logging.getLogger(self.__class__.__name__)

        # TODO: Add weighted average.  Might be better than using the range.

        self.name = name
        self.length = length
        self.max = 0
        self.min = 0
        self.range = 0
        self.average = 0
        self.printDebug = printDebug

        # TODO: Isn't there a more Pythonic way to do this?
        if data:
            super(AveragedList, self).__init__(data)
            self._updateStats()
        else:
            super(AveragedList, self).__init__()

    def __str__(self):
        return 'name:%s average:%s range:%s max:%s min:%s' % (
            self.name, self.average, self.range, self.max, self.min)

    __repr__ = __str__

    def append(self, arg):
        arg = MyFloat(arg)
        super(AveragedList, self).append(arg)
        self._updateStats()

    def clear(self):
        '''Empties the list.'''

        while len(self) > 0:
            self.pop()

    def extend(self, *args):
        args = [[MyFloat(a) for l in args for a in l]]
        super(AveragedList, self).extend(*args)
        self._updateStats()

    def insert(self, pos, *args):
        args = [MyFloat(a) for a in args]
        super(AveragedList, self).insert(pos, *args)

        while len(self) > self.length:
            self.pop()
        self._updateStats()

    def _updateStats(self):
        self.average = MyFloat(sum(self) / len(self))
        self.max = MyFloat(max(self))
        self.min = MyFloat(min(self))
        self.range = MyFloat(self.max - self.min)

        if self.printDebug:
            self.log.debug(self)

class Client(mpd.MPDClient):
    '''Subclasses mpd.MPDClient, keeping state data, reconnecting as
    needed, etc.'''

    initAttrs = {None: ['currentStatus', 'lastSong',
                        'currentSongFiletype', 'playlist',
                        'playlistVersion', 'playlistLength',
                        'song', 'duration', 'elapsed', 'state',
                        'hasBeenSynced', 'playing', 'paused'],
                 False: ['consume', 'random', 'repeat',
                         'single']}

    def __init__(self, host, port=DEFAULT_PORT, password=None, latency=None,
                 logger=None):

        super(Client, self).__init__()

        # Command timeout
        self.timeout = 10

        # Split host/latency
        if '/' in host:
            host, latency = host.split('/')

        if latency is not None:
            self.latency = float(latency)
        else:
            self.latency = None

        # Split host/port
        if ':' in host:
            host, port = host.split(':')

        self.host = host
        self.port = port
        self.password = password

        self.log = logger.getChild('%s(%s)' %
                                   (self.__class__.__name__, self.host))

        self.syncLoopLocked = False
        self.playedSinceLastPlaylistUpdate = False

        self.currentSongShouldSeek = True
        self.currentSongAdjustments = 0
        self.currentSongDifferences = AveragedList(
            name='currentSongDifferences', length=10)

        self.pings = AveragedList(name='%s.pings' % self.host, length=10)
        self.adjustments = AveragedList(name='%sadjustments' % self.host,
                                        length=20)
        self.initialPlayTimes = AveragedList(name='%s.initialPlayTimes'
                                             % self.host, length=20,
                                             printDebug=True)

        # MAYBE: Should I reset this in _initAttrs() ?
        self.reSeekedTimes = 0

        # Record adjustments by file type to see if there's a pattern
        self.fileTypeAdjustments = defaultdict(AveragedList)

        # TODO: Record each song's number of adjustments in a list (by
        # filename), and print on exit.  This way I can play a short
        # playlist in a loop and see if there is a pattern with
        # certain songs being consistently bad at syncing and seeking.

    def ping(self):
        '''Pings the daemon and records how long it took.'''

        self.pings.insert(0, timeFunction(super(Client, self).ping))

    def checkConnection(self):
        '''Pings the daemon and tries to reconnect if necessary.'''

        # I don't know why this is necessary, but for some reason the
        # slave connections tend to get dropped.
        try:
            self.ping()

        except Exception as e:
            self.log.debug('Connection to "%s" seems to be down.  '
                           'Trying to reconnect...', self.host)

            # Try to disconnect first
            try:
                self.disconnect()  # Maybe this will help it reconnect
            except Exception as e:
                self.log.exception("Couldn't DISconnect from client %s: %s",
                                   self.host, e)

            # Try to reconnect
            try:
                self.connect()
            except Exception as e:
                self.log.critical('Unable to reconnect to "%s"', self.host)

                return False
            else:
                self.log.debug('Reconnected to "%s"', self.host)

                return True

        else:
            self.log.debug("Connection still up to %s", self.host)

            return True

    def connect(self):
        '''Connects to the daemon, sets the password if necessary, and tests
        the ping time.'''

        # Reset initial values
        for val, attrs in self.initAttrs.iteritems():
            for attr in attrs:
                setattr(self, attr, val)

        super(Client, self).connect(self.host, self.port)

        if self.password:
            super(Client, self).password(self.password)

        self.testPing()

    def getPlaylist(self):
        '''Gets the playlist from the daemon.'''

        self.playlist = super(Client, self).playlist()

    def pause(self):
        '''Pauses the daemon and tracks the playing state.'''

        super(Client, self).pause()
        self.playing = False
        self.paused = True

    def play(self, initial=False):
        '''Plays the daemon, adjusting starting position as necessary.'''

        # FIXME: I was checking if (self.playedSinceLastPlaylistUpdate
        # == False), but I removed that code.  I'm not sure if it's
        # still necessary.

        if initial:
            # Slave is not already playing, or is playing a different song
            self.log.debug("%s.play(initial=True)", self.host)

            # Calculate adjustment
            if self.latency is not None:
                # Use user-set adjustment
                adjustBy = self.latency
            elif self.initialPlayTimes.average:
                self.log.debug("Adjusting by average initial play time")

                adjustBy = self.initialPlayTimes.average
            else:
                self.log.debug("Adjusting by average ping")

                adjustBy = self.pings.average

            self.log.debug('Adjusting initial play by %s seconds', adjustBy)

            # Update status (not sure if this is still necessary, but
            # it might help avoid race conditions or something)
            self.status()

            # Execute in command list
            # TODO: Is a command list necessary or helpful here?
            try:
                self.command_list_ok_begin()
            except mpd.CommandListError as e:
                # Server was already in a command list; probably a
                # lost client connection, so try again
                self.log.exception("mpd.CommandListError: %s", e)

                self.command_list_end()
                self.command_list_ok_begin()

            # Adjust starting position if necessary
            # TODO: Is it necessary or good to make sure it's a
            # positive adjustment?  There seem to be some tracks that
            # require negative adjustments, but I don't know if that
            # would be the case when playing from a stop
            if adjustBy > 0:
                tries = 0

                # Wait for the server to...catch up?  I don't remember
                # exactly why this code is here, because it seems like
                # the master shouldn't be behind the slaves, but I
                # suppose it could happen on song changes
                while self.elapsed is None and tries < 10:
                    time.sleep(0.2)
                    self.status()
                    self.log.debug(self.song)
                    tries += 1

                # Seek to the adjusted playing position
                self.seek(self.song, self.elapsed + adjustBy)

            # Issue the play command
            super(Client, self).play()

            # Execute command list
            result = self.command_list_end()

        else:
            # Slave is already playing current song
            self.log.debug("%s.play(initial=False)", self.host)

            # Issue the play command
            result = super(Client, self).play()

        # TODO: Not sure if this is still necessary to track...
        self.playedSinceLastPlaylistUpdate = True

        return result

    def seek(self, song, elapsed):
        '''Seeks daemon to a position and updates local attributes for current
        song and elapsed time.'''

        self.song = song
        self.elapsed = elapsed
        super(Client, self).seek(self.song, self.elapsed)

    def status(self):
        '''Gets daemon's status and updates local attributes.'''

        self.currentStatus = super(Client, self).status()

        # Wrap whole thing in try/except because of MPD protocol
        # errors.  But I may have fixed this by "locking" each client
        # in the loop, so this may not be necessary anymore.
        try:

            # Not sure why, but sometimes this ends up as None when
            # the track or playlist is changed...?
            if self.currentStatus:
                # Status response received

                # Set playlist attrs
                self.playlistLength = int(self.currentStatus['playlistlength'])
                if self.playlist:
                    self.currentSongFiletype = (
                        self.playlist[int(self.song)].split('.')[-1])

                    self.log.debug('Current filetype: %s',
                                   self.currentSongFiletype)

                # Set True/False attrs
                for attr in self.initAttrs[False]:
                    val = (True
                           if self.currentStatus[attr] == '1'
                           else False)
                    setattr(self, attr, val)

                # Set playing state attrs
                self.state = self.currentStatus['state']
                self.playing = (True
                                if self.state == 'play'
                                else False)
                self.paused = (True
                               if self.state == 'pause'
                               else False)

                # Set song attrs
                self.song = (self.currentStatus['song']
                             if 'song' in self.currentStatus
                             else None)
                for attr in ['duration', 'elapsed']:
                    val = (MyFloat(self.currentStatus[attr])
                           if attr in self.currentStatus
                           else None)
                    setattr(self, attr, val)

            else:
                # None?  Sigh...  This shouldn't happen...if it does
                # I'll need to reconnect, I think...
                self.log.error("No status received for client %s", self.host)

        except Exception as e:
            # No status response :(
            self.log.exception("Unable to get status for client %s: %s",
                               self.host, e)

            # Try to reconnect
            self.checkConnection()

        # TODO: Add other attributes, e.g. {'playlistlength': '55',
        # 'playlist': '3868', 'repeat': '0', 'consume': '0',
        # 'mixrampdb': '0.000000', 'random': '0', 'state': 'stop',
        # 'volume': '-1', 'single': '0'}

    def testPing(self):
        '''Pings the daemon 5 times and sets the initial maxDifference.'''

        for i in range(5):
            self.ping()
            time.sleep(0.1)

        self.maxDifference = self.pings.average * 5

        self.log.debug('Average ping for %s: %s seconds; '
                       'setting maxDifference: %s',
                       self.host, self.pings.average, self.maxDifference)

# ** Functions
def timeFunction(f):
    t1 = time.time()
    f()
    t2 = time.time()
    return t2 - t1

def main():

    # Parse args
    parser = argparse.ArgumentParser(
            description='Trims an MPD queue to a desired length')
    parser.add_argument('-l', '--length', help="Desired length of queue in minutes")
    parser.add_argument('-d', '--daemon', default='localhost', dest='host',
                        help='Name or address of server, optionally with port in HOST:PORT format.  Default: localhost:6600')
    parser.add_argument('-g', '--genre')
    parser.add_argument("-v", "--verbose", action="count", dest="verbose", help="Be verbose, up to -vvv")
    args = parser.parse_args()
    
    # Setup logging
    log = logging.getLogger('trim-mpd-queue')
    if args.verbose >= 3:
        # Debug everything, including MPD module.  This sets the root
        # logger, which python-mpd2 uses.  Too bad it doesn't use a
        # logging.NullHandler to make this cleaner.  See
        # https://docs.python.org/2/howto/logging.html#library-config
        LOG_LEVEL = logging.DEBUG
        logging.basicConfig(level=LOG_LEVEL, format="%(levelname)s: %(name)s: %(message)s")

    else:
        # Don't debug MPD.  Don't set the root logger.  Do manually
        # what basicConfig() does, because basicConfig() sets the root
        # logger.  This seems more confusing than it should be.  I
        # think the key is that logging.logger.getChild() is not in
        # the logging howto tutorials.  When I found getChild() (which
        # is in the API docs, which are also not obviously linked in
        # the howto), it started falling into place.  But without
        # getchild(), it was a confusing mess.
        if args.verbose == 1:
            LOG_LEVEL = logging.INFO
        elif args.verbose == 2:
            LOG_LEVEL = logging.DEBUG
        else:
            LOG_LEVEL = logging.WARNING

        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(levelname)s: %(name)s: %(message)s"))
        log.addHandler(handler)
        log.setLevel(LOG_LEVEL)

    log.debug('Using python-mpd version: %s', str(mpd.VERSION))
    log.debug("Args: %s", args)

    # Check args
    if not args.genre:
        log.error("Please give a genre.")
        return False

    # Connect to the master server
    daemon = Client(host=args.host, port=DEFAULT_PORT, logger=log)

    try:
        daemon.connect()
    except Exception as e:
        log.exception('Unable to connect to master server: %s', e)
        return False
    else:
        log.debug('Connected to master server.')

    # Find songs
    pool = Playlist(*daemon.search('genre', args.genre))

    if not pool:
        log.error('No songs found.')
        return False

    # Check result
    if not pool:
        log.error("No tracks remaining to use.")
        return False

    log.debug("Pool: %s tracks, %s seconds" % (len(pool), pool.duration))
        
    # Build new playlist
    originalPool = Playlist(*pool)
    newPlaylist = Playlist()
    numInputTracks = len(pool)

    # *** Using length
    if args.length:

        args.length = int(args.length) * 60  # Convert length to seconds

        if pool.duration < (args.length - 30):  # If the pool is shorter than the desired duration, it will be necessary to repeat some tracks
            allowDuplicates = True
            log.debug('Track pool duration (%s seconds) shorter than desired length (%s seconds); will allow duplicate tracks in output' % (pool.duration, args.length))
            newPlaylist = Playlist(*pool)  # Start with all the tracks

        else:
            allowDuplicates = False
            log.debug('Not allowing duplicate tracks in output')        

        tries = 1
        while True:
            remainingTime = args.length - newPlaylist.duration

            # Isn't there some way to do this in the while condition in Python?
            tracksThatFit = [track for track in pool if int(track['time']) < remainingTime]  # Can I/should I use a set comprehension instead of a listcomp?

            log.debug("Tracks that fit in remaining time of %s seconds: %s" % (remainingTime, len(tracksThatFit)))
            
            # Are we there yet?
            if not tracksThatFit:
                log.debug("No tracks remaining that fit in remaining time of %s seconds" % (remainingTime))

                # If not within 30 seconds of desired time, start over
                if (args.length - newPlaylist.duration > 30):

                    # TODO: Increase margin gradually. This will help
                    # prevent situations where, e.g. the desired
                    # length is 25 minutes, but the closest it can get
                    # is 24 minutes, and after the 10 tries, it
                    # happens to go with one that's only 21 minutes
                    # long instead of 24.
                    if tries == len(originalPool):
                        log.warning("Tried %s times to make a playlist within 30 seconds of the desired length; gave up and made one %s seconds long."
                                    % (tries, newPlaylist.duration))
                        break

                    log.debug("Not within 30 seconds of desired playlist length.  Trying again...")
                    
                    if not allowDuplicates:
                        pool = Playlist()
                        pool.extend(originalPool)
                        newPlaylist = Playlist()
                    else:
                        # Add all tracks to playlist
                        newPlaylist = Playlist(*pool)

                    tries += 1

                # We are there yet.
                else:
                    log.debug("Took %s tries to make playlist" % tries)

                    break

            # Keep going
            else:
                newTrack = random.choice(tracksThatFit)
                newPlaylist.append(newTrack)
                log.debug("Adding track: %s" % newTrack)
                if not allowDuplicates:
                    pool.remove(newTrack)


    else:
        # No length; use all tracks            
        newPlaylist = Playlist(*pool)

        # TODO: Shuffle it since it doesn't get created randomly
        
    # Add tracks to mpd
    daemon.clear()
    daemon.command_list_ok_begin()
    for track in newPlaylist:
        daemon.add(track['file'].replace('file: ', ''))

    daemon.command_list_end()

    daemon.play()

    if args.length:
        log.info("New playlist duration: %i of %s desired seconds" % (newPlaylist.duration, args.length))
        log.info("Used %i (%i%%) of %i tracks" % ( len(newPlaylist), (round(len(newPlaylist) / numInputTracks, 2)) * 100, numInputTracks))
    else:
        hours = newPlaylist.duration // 3600
        minutes = newPlaylist.duration // 60 % 60
        seconds = newPlaylist.duration % 60 % 60
        log.info("New playlist: %s tracks, %ih:%im:%is" % (numInputTracks, hours, minutes, seconds))

    return True

    
if __name__ == '__main__':
    sys.exit(main())
