#!/usr/bin/env python

# * mpd-search-add.py

# ** Imports
import argparse
import logging
import os
import random
import re
import sys

import mpd  # Using python-mpd2

# Verify python-mpd2 is being used
if mpd.VERSION < (0, 5, 4):
    print 'ERROR: This script requires python-mpd2 >= 0.5.4.'
    sys.exit(1)

# ** Constants
DEFAULT_PORT = 6600
FILE_PREFIX_RE = re.compile('^file: ')


# ** Classes


class Track(object):
    def __init__(self, duration=None, title=None, path=None):
        self.duration = int(duration)
        self.title = title
        self.path = path

    # These two are the magic that makes sets work
    def __eq__(self, other):
        return self.path == other.path

    def __hash__(self):
        return hash(self.path)

    def __str__(self):
        return os.path.basename(self.path)


class Playlist(list):
    def __init__(self, *args, **kwargs):
        super(Playlist, self).__init__(args)
        self.duration = sum([track.duration for track in args]) if args else 0

    def append(self, item):
        super(Playlist, self).append(item)

        # TODO: Is there a more Pythonic way to do this?
        self.duration += (sum([track.duration for track in item])
                          if isinstance(item, list)
                          or isinstance(item, set)
                          else item.duration)

    def extend(self, item):
        super(Playlist, self).extend(item)

        # TODO: Is there a more Pythonic way to do this?


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

        # Issue the play command
        result = super(Client, self).play()

        return result

    def seek(self, song, elapsed):
        '''Seeks daemon to a position and updates local attributes for current
        song and elapsed time.'''

        self.song = song
        self.elapsed = elapsed
        super(Client, self).seek(self.song, self.elapsed)


# ** Functions

def main():

    # *** Parse args
    parser = argparse.ArgumentParser(
        description='Search for tracks in an MPD library and'
        'add them to its playlist')

    # TODO: parse any number, with or without 'm' or 'h' at the end,
    # as length in minutes or hours
    parser.add_argument('-d', '--duration', metavar="MINUTES",
                        help="Desired duration of queue in minutes")
    parser.add_argument('-s', '--server', default='localhost', dest='host',
                        help='Name or address of server, optionally with'
                        'port in HOST:PORT format.  Default: localhost:6600')

    # TODO: Use action='append' and flatten resulting lists
    parser.add_argument('-A', '--any', nargs='*')
    parser.add_argument('-a', '--artists', dest='artist', nargs='*')
    parser.add_argument('-b', '--albums', dest='album', nargs='*')
    parser.add_argument('-t', '--titles', dest='title', nargs='*')
    parser.add_argument('-g', '--genres', dest='genre', nargs='*')

    parser.add_argument('-p', '--print-filenames',
                        dest='printFilenames', action="store_true")

    parser.add_argument("-v", "--verbose", action="count", dest="verbose",
                        help="Be verbose, up to -vvv")
    args = parser.parse_args()

    queries = ['any', 'artist', 'album', 'title', 'genre']

    # *** Setup logging
    log = logging.getLogger('trim-mpd-queue')
    if args.verbose >= 3:
        # Debug everything, including MPD module.  This sets the root
        # logger, which python-mpd2 uses.  Too bad it doesn't use a
        # logging.NullHandler to make this cleaner.  See
        # https://docs.python.org/2/howto/logging.html#library-config
        LOG_LEVEL = logging.DEBUG
        logging.basicConfig(level=LOG_LEVEL,
                            format="%(levelname)s: %(name)s: %(message)s")

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
        handler.setFormatter(
            logging.Formatter("%(levelname)s: %(name)s: %(message)s"))
        log.addHandler(handler)
        log.setLevel(LOG_LEVEL)

    log.debug('Using python-mpd version: %s', str(mpd.VERSION))
    log.debug("Args: %s", args)

    # *** Check args
    found = False
    for q in queries:
        if getattr(args, q):
            found = True
            break

    if not found:
        log.error("Please give a query.")
        return False

    # *** Connect to the master server
    daemon = Client(host=args.host, port=DEFAULT_PORT, logger=log)

    try:
        daemon.connect()
    except Exception as e:
        log.exception('Unable to connect to master server: %s', e)
        return False
    else:
        log.debug('Connected to master server.')

    # *** Find songs
    pools = []

    for queryType in queries:
        if getattr(args, queryType):
            for query in getattr(args, queryType):
                pools.append(Playlist(
                    *[Track(duration=track['time'],  # Unpack listcomp of Tracks
                            path=track['file'].replace('file: ', ''))  # Trim file string
                      for track in daemon.search(queryType, query)]))

    # Check result
    if not any(pools):
        log.error("No tracks found for queries.")
        return False

    log.debug("Pool: %s tracks, %s seconds" % (
        sum(map(len, pools)),
        sum([track.duration
             for pool in pools
             for track in pool
             if track.duration > 0])))

    # Build new playlist without dupes

    # Test the track duration. I found one track that had a very
    # strange duration, a huge negative number, and it messed up the
    # script and caused an infinite loop.
    originalPool = Playlist(
        *set([Track(duration=track.duration,  # Unpack the set
                    path=track.path)
              for pool in pools
              for track in pool
              if track.duration > 0]))
    newPlaylist = Playlist()
    numInputTracks = len(originalPool)

    pool = Playlist(*originalPool)

    # *** Using duration
    if args.duration:

        # Convert duration from minutes to seconds
        args.duration = int(args.duration) * 60

        if pool.duration < (args.duration - 30):
            # If the pool is shorter than the desired duration, it
            # will be necessary to repeat some tracks
            allowDuplicates = True
            log.debug('Track pool duration (%s seconds) shorter than desired duration (%s seconds);'
                      'will allow duplicate tracks in output',
                      pool.duration, args.duration)
            newPlaylist = Playlist(*pool)  # Start with all the tracks

        else:
            allowDuplicates = False
            log.debug('Not allowing duplicate tracks in output')

        tries = 1
        while True:
            remainingTime = args.duration - newPlaylist.duration

            # Isn't there some way to do this in the while condition in Python?
            tracksThatFit = [Track(duration=track.duration, path=track.path)
                             for track in pool
                             if int(track.duration) < remainingTime]

            log.debug("Tracks that fit in remaining time of %s seconds: %s",
                      remainingTime, len(tracksThatFit))

            # Are we there yet?
            if not tracksThatFit:
                log.debug("No tracks remaining that fit in remaining time of %s seconds",
                          remainingTime)

                if (args.duration - newPlaylist.duration > 30):
                    # If not within 30 seconds of desired time, start over

                    # TODO: Increase margin gradually. This will help
                    # prevent situations where, e.g. the desired
                    # duration is 25 minutes, but the closest it can get
                    # is 24 minutes, and after the 10 tries, it
                    # happens to go with one that's only 21 minutes
                    # long instead of 24.
                    if tries == len(originalPool):
                        log.warning("Tried %s times to make a playlist within 30 seconds"
                                    "of the desired duration; gave up and made one %s seconds long.",
                                    tries, newPlaylist.duration)
                        break

                    log.debug("Not within 30 seconds of desired playlist duration.  Trying again...")

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
        # *** No duration; use all tracks
        newPlaylist = Playlist(*pool)

        # TODO: Shuffle it since it doesn't get created randomly

    # *** Add tracks to mpd or print
    if args.printFilenames:
        # Just print filenames to STDOUT
        print "\n".join([track.path for track in newPlaylist])

    else:
        # Add tracks to MPD
        daemon.clear()

        daemon.command_list_ok_begin()
        for track in newPlaylist:
            daemon.add(track.path)

        daemon.command_list_end()

        daemon.play()

        # TODO: Send these to STDERR so they can be used with -p
        # without interfering
        if args.duration:
            log.info("New playlist duration: %i of %s desired seconds",
                     newPlaylist.duration, args.duration)
            log.info("Used %i (%i%%) of %i tracks",
                     len(newPlaylist),
                     (round(len(newPlaylist) / numInputTracks, 2)) * 100,
                     numInputTracks)
        else:
            hours = newPlaylist.duration // 3600
            minutes = newPlaylist.duration // 60 % 60
            seconds = newPlaylist.duration % 60 % 60
            log.info("New playlist: %s tracks, %ih:%im:%is",
                     numInputTracks, hours, minutes, seconds)

    return True


if __name__ == '__main__':
    sys.exit(0 if main() else 1)
